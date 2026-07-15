"""Per-contact owner-only durable queue for exact private link evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Mapping

_QUEUE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS queue_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS link_job(
  job_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  url_id TEXT NOT NULL,
  exact_url TEXT NOT NULL,
  occurred_at REAL NOT NULL,
  recency_bucket INTEGER NOT NULL CHECK(recency_bucket BETWEEN 0 AND 3),
  engagement_score INTEGER NOT NULL,
  repeated_shares INTEGER NOT NULL CHECK(repeated_shares >= 1),
  distinct_days INTEGER NOT NULL CHECK(distinct_days >= 1),
  state TEXT NOT NULL CHECK(state IN ('pending','claimed','retry_wait','complete','failed','retracted')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  not_before REAL NOT NULL DEFAULT 0,
  claim_token TEXT,
  claim_until REAL,
  failure_code TEXT,
  result_commitment TEXT,
  result_json TEXT,
  projection_generation INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT (unixepoch()),
  updated_at REAL NOT NULL DEFAULT (unixepoch()),
  UNIQUE(event_id,url_id)
);
CREATE INDEX IF NOT EXISTS link_job_ready
  ON link_job(state,not_before,recency_bucket,engagement_score,occurred_at);
"""
_OPAQUE = re.compile(r"[0-9a-f]{64}")
_FAILURE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class ClaimedLinkJob:
    job_id: str
    event_id: str
    url_id: str
    exact_url: str
    occurred_at: float
    attempt_count: int
    claim_token: str
    engagement_score: int
    repeated_shares: int
    distinct_days: int
    projection_generation: int
    failure_code: str | None
    result_commitment: str | None
    result_json: str | None


def queue_path(root: str | Path, contact_id: str) -> Path:
    identity = hashlib.sha256(("private-link-queue-v1\0" + str(contact_id)).encode()).hexdigest()
    return Path(root).expanduser().resolve() / "private-link-research" / f"{identity}.sqlite3"


class PrivateLinkResearchQueue:
    """The exact-URL sidecar is physically isolated from the canonical contact DB."""

    def __init__(self, root: str | Path, contact_id: str, *, timeout: float = 5.0) -> None:
        if not str(contact_id).strip():
            raise ValueError("contact_id is required")
        self.contact_id = str(contact_id)
        self.path = queue_path(root, self.contact_id)
        self.timeout = min(max(float(timeout), 0.1), 30.0)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self._connect() as con:
            con.executescript(_QUEUE_SCHEMA)
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(link_job)")}
            if "result_json" not in columns:
                con.execute("ALTER TABLE link_job ADD COLUMN result_json TEXT")
            if "projection_generation" not in columns:
                con.execute(
                    "ALTER TABLE link_job ADD COLUMN projection_generation INTEGER NOT NULL DEFAULT 0"
                )
            con.execute(
                "INSERT INTO queue_meta(key,value) VALUES('schema_version','1') "
                "ON CONFLICT(key) DO NOTHING"
            )
            con.execute(
                "INSERT INTO queue_meta(key,value) VALUES('contact_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.contact_id,),
            )
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA secure_delete=ON")
        con.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        for suffix in ("", "-wal", "-shm"):
            member = Path(str(self.path) + suffix)
            if member.exists():
                os.chmod(member, stat.S_IRUSR | stat.S_IWUSR)
        return con

    @staticmethod
    def _identity(value: str, name: str) -> str:
        if not _OPAQUE.fullmatch(str(value)):
            raise ValueError(f"{name} must be an opaque identity")
        return str(value)

    def enqueue(
        self,
        *,
        job_id: str,
        event_id: str,
        url_id: str,
        exact_url: str,
        occurred_at: float,
        recency_bucket: int,
        engagement_score: int,
        repeated_shares: int,
        distinct_days: int,
    ) -> bool:
        values = (
            self._identity(job_id, "job_id"),
            self._identity(event_id, "event_id"),
            self._identity(url_id, "url_id"),
            str(exact_url),
            float(occurred_at),
            int(recency_bucket),
            int(engagement_score),
            int(repeated_shares),
            int(distinct_days),
        )
        if not values[3].startswith("https://"):
            raise ValueError("queue accepts only admitted HTTPS URLs")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                prior = con.execute(
                    "SELECT event_id,url_id,exact_url,occurred_at,recency_bucket,engagement_score,"
                    "repeated_shares,distinct_days FROM link_job WHERE job_id=?",
                    (values[0],),
                ).fetchone()
                if prior is not None:
                    expected = values[1:6]
                    actual = tuple(prior)[:5]
                    if actual != expected:
                        raise ValueError("job_id already belongs to different private evidence")
                    con.execute("COMMIT")
                    return False
                con.execute(
                    "INSERT INTO link_job(job_id,event_id,url_id,exact_url,occurred_at,recency_bucket,"
                    "engagement_score,repeated_shares,distinct_days,state) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                    values,
                )
                con.execute("COMMIT")
                return True
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise

    def claim_next(
        self,
        *,
        now: float,
        lease_seconds: float,
        eventual_old_every: int = 10,
    ) -> ClaimedLinkJob | None:
        timestamp = float(now)
        lease = min(max(float(lease_seconds), 1.0), 3600.0)
        cadence = max(int(eventual_old_every), 1)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "UPDATE link_job SET state='retry_wait',claim_token=NULL,claim_until=NULL "
                    "WHERE state='claimed' AND claim_until<=?",
                    (timestamp,),
                )
                completed = int(con.execute(
                    "SELECT COUNT(*) FROM link_job WHERE state IN ('complete','failed','retracted')"
                ).fetchone()[0])
                old_turn = (completed + 1) % cadence == 0
                ordering = (
                    "recency_bucket DESC,occurred_at ASC,job_id ASC"
                    if old_turn else
                    "recency_bucket ASC,engagement_score DESC,repeated_shares DESC,"
                    "distinct_days DESC,occurred_at DESC,job_id ASC"
                )
                row = con.execute(
                    "SELECT * FROM link_job WHERE state IN ('pending','retry_wait') AND not_before<=? "
                    f"ORDER BY {ordering} LIMIT 1",
                    (timestamp,),
                ).fetchone()
                if row is None:
                    con.execute("COMMIT")
                    return None
                token = secrets.token_hex(32)
                changed = con.execute(
                    "UPDATE link_job SET state='claimed',attempt_count=attempt_count+1,claim_token=?,"
                    "claim_until=?,updated_at=? WHERE job_id=? AND state IN ('pending','retry_wait')",
                    (token, timestamp + lease, timestamp, row["job_id"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("private link claim race")
                con.execute("COMMIT")
                return ClaimedLinkJob(
                    job_id=str(row["job_id"]), event_id=str(row["event_id"]),
                    url_id=str(row["url_id"]), exact_url=str(row["exact_url"]),
                    occurred_at=float(row["occurred_at"]),
                    attempt_count=int(row["attempt_count"]) + 1, claim_token=token,
                    engagement_score=int(row["engagement_score"]),
                    repeated_shares=int(row["repeated_shares"]),
                    distinct_days=int(row["distinct_days"]),
                    projection_generation=int(row["projection_generation"]),
                    failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
                    result_commitment=(
                        str(row["result_commitment"]) if row["result_commitment"] else None
                    ),
                    result_json=(str(row["result_json"]) if row["result_json"] else None),
                )
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise

    def _finish(
        self,
        job_id: str,
        claim_token: str,
        *,
        state: str,
        now: float,
        failure_code: str | None = None,
        result_commitment: str | None = None,
        result_json: str | None = None,
        not_before: float | None = None,
    ) -> None:
        if failure_code is not None and not _FAILURE.fullmatch(failure_code):
            raise ValueError("failure_code must be a stable machine token")
        if result_commitment is not None:
            self._identity(result_commitment, "result_commitment")
        with self._connect() as con:
            changed = con.execute(
                "UPDATE link_job SET state=?,not_before=?,claim_token=NULL,claim_until=NULL,"
                "failure_code=?,result_commitment=?,result_json=?,updated_at=? "
                "WHERE job_id=? AND state='claimed' AND claim_token=? AND claim_until>?",
                (
                    state, float(not_before if not_before is not None else now), failure_code,
                    result_commitment, result_json, float(now),
                    self._identity(job_id, "job_id"), str(claim_token), float(now),
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("claim token is stale or does not own the job")

    def complete(
        self, job_id: str, claim_token: str, *, result_commitment: str,
        now: float, result_json: str | None = None,
    ) -> None:
        self._finish(
            job_id, claim_token, state="complete", now=now,
            result_commitment=result_commitment, result_json=result_json,
        )

    def stage_result(
        self, job_id: str, claim_token: str, *, result_commitment: str,
        result_json: str, now: float,
    ) -> None:
        """Durably stage researched output before canonical projection is attempted."""
        with self._connect() as con:
            changed = con.execute(
                "UPDATE link_job SET failure_code='canonical_apply_pending',"
                "result_commitment=?,result_json=?,updated_at=? "
                "WHERE job_id=? AND state='claimed' AND claim_token=?",
                (
                    self._identity(result_commitment, "result_commitment"), str(result_json),
                    float(now), self._identity(job_id, "job_id"), str(claim_token),
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("claim token is stale or does not own the job")

    def retry_staged(self, job_id: str, claim_token: str, *, now: float) -> None:
        """Retry canonical apply without discarding researched output."""
        with self._connect() as con:
            changed = con.execute(
                "UPDATE link_job SET state='retry_wait',not_before=?,claim_token=NULL,"
                "claim_until=NULL,failure_code='canonical_apply_pending',updated_at=? "
                "WHERE job_id=? AND state='claimed' AND claim_token=? AND result_json IS NOT NULL",
                (
                    float(now) + 1.0, float(now), self._identity(job_id, "job_id"),
                    str(claim_token),
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("claim token is stale or has no staged result")

    def retry(self, job_id: str, claim_token: str, *, failure_code: str, now: float) -> None:
        with self._connect() as con:
            row = con.execute("SELECT attempt_count FROM link_job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("unknown private link job")
        attempt = int(row[0])
        if attempt >= 4:
            self._finish(job_id, claim_token, state="failed", now=now, failure_code=failure_code)
            return
        delay = (1.0, 4.0, 16.0)[min(attempt - 1, 2)]
        self._finish(
            job_id, claim_token, state="retry_wait", now=now,
            failure_code=failure_code, not_before=float(now) + delay,
        )

    def retract_event(self, event_id: str, *, now: float) -> int:
        with self._connect() as con:
            return con.execute(
                "UPDATE link_job SET state='retracted',updated_at=? WHERE event_id=? "
                "AND state!='retracted'",
                (float(now), self._identity(event_id, "event_id")),
            ).rowcount

    def engage_event(self, event_id: str, *, score: int, now: float) -> int:
        normalized = max(0, int(score))
        with self._connect() as con:
            return con.execute(
                "UPDATE link_job SET engagement_score=?,"
                "state=CASE WHEN ? > 0 AND state='complete' THEN 'pending' ELSE state END,"
                "projection_generation=CASE WHEN ? > 0 AND state='complete' "
                "THEN projection_generation+1 ELSE projection_generation END,"
                "not_before=CASE WHEN ? > 0 THEN ? ELSE not_before END,updated_at=? "
                "WHERE event_id=? AND state NOT IN ('failed','retracted')",
                (
                    normalized, normalized, normalized, normalized, float(now), float(now),
                    self._identity(event_id, "event_id"),
                ),
            ).rowcount

    def completed_for_url(self, exact_url: str) -> list[sqlite3.Row]:
        with self._connect() as con:
            return list(con.execute(
                "SELECT event_id,result_json,result_commitment,engagement_score,repeated_shares,distinct_days "
                "FROM link_job WHERE exact_url=? AND state='complete' ORDER BY occurred_at,event_id",
                (str(exact_url),),
            ))

    def completed_for_event(self, event_id: str) -> list[sqlite3.Row]:
        with self._connect() as con:
            return list(con.execute(
                "SELECT url_id,result_json,result_commitment FROM link_job "
                "WHERE event_id=? AND state='complete' ORDER BY url_id",
                (self._identity(event_id, "event_id"),),
            ))

    def refresh_claim(self, job_id: str, claim_token: str, *, now: float) -> ClaimedLinkJob | None:
        """Re-read mutable qualification state and reject stale/retracted ownership."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM link_job WHERE job_id=? AND state='claimed' "
                "AND claim_token=? AND claim_until>?",
                (self._identity(job_id, "job_id"), str(claim_token), float(now)),
            ).fetchone()
        if row is None:
            return None
        return ClaimedLinkJob(
            job_id=str(row["job_id"]), event_id=str(row["event_id"]),
            url_id=str(row["url_id"]), exact_url=str(row["exact_url"]),
            occurred_at=float(row["occurred_at"]), attempt_count=int(row["attempt_count"]),
            claim_token=str(row["claim_token"]), engagement_score=int(row["engagement_score"]),
            repeated_shares=int(row["repeated_shares"]), distinct_days=int(row["distinct_days"]),
            projection_generation=int(row["projection_generation"]),
            failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
            result_commitment=(
                str(row["result_commitment"]) if row["result_commitment"] else None
            ),
            result_json=(str(row["result_json"]) if row["result_json"] else None),
        )

    def renew_claim(
        self, job_id: str, claim_token: str, *, now: float, lease_seconds: float = 300.0,
    ) -> ClaimedLinkJob | None:
        """Validate unexpired ownership and extend its lease around slow work."""
        timestamp = float(now)
        lease = min(max(float(lease_seconds), 1.0), 3600.0)
        with self._connect() as con:
            changed = con.execute(
                "UPDATE link_job SET claim_until=?,updated_at=? WHERE job_id=? "
                "AND state='claimed' AND claim_token=? AND claim_until>?",
                (
                    timestamp + lease, timestamp, self._identity(job_id, "job_id"),
                    str(claim_token), timestamp,
                ),
            ).rowcount
        if changed != 1:
            return None
        return self.refresh_claim(job_id, claim_token, now=timestamp)

    def event_state(self, event_id: str) -> tuple[str, int, int] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT state,engagement_score,projection_generation FROM link_job WHERE event_id=?",
                (self._identity(event_id, "event_id"),),
            ).fetchone()
        return None if row is None else (
            str(row["state"]), int(row["engagement_score"]), int(row["projection_generation"]),
        )

    def reopen_completed(
        self, job_id: str, *, result_commitment: str, failure_code: str, now: float,
    ) -> bool:
        """Return a completed claim to retry only while this result still owns it."""
        if not _FAILURE.fullmatch(failure_code):
            raise ValueError("failure_code must be a stable machine token")
        with self._connect() as con:
            changed = con.execute(
                "UPDATE link_job SET state='retry_wait',failure_code=?,not_before=?,updated_at=? "
                "WHERE job_id=? AND state='complete' AND result_commitment=?",
                (
                    failure_code, float(now) + 1.0, float(now),
                    self._identity(job_id, "job_id"),
                    self._identity(result_commitment, "result_commitment"),
                ),
            ).rowcount
        return changed == 1

    def pending_count(self) -> int:
        with self._connect() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM link_job WHERE state IN ('pending','claimed','retry_wait')"
            ).fetchone()[0])

    def stats(self) -> Mapping[str, int]:
        with self._connect() as con:
            return {str(row[0]): int(row[1]) for row in con.execute(
                "SELECT state,COUNT(*) FROM link_job GROUP BY state ORDER BY state"
            )}


def discover_contacts_with_queues(root: str | Path) -> tuple[str, ...]:
    directory = Path(root).expanduser().resolve() / "private-link-research"
    if not directory.is_dir():
        return ()
    contacts: set[str] = set()
    for path in sorted(directory.glob("*.sqlite3")):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                continue
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT value FROM queue_meta WHERE key='contact_id'"
                ).fetchone()
            finally:
                con.close()
            if row is not None and str(row[0]).strip():
                contacts.add(str(row[0]))
        except (OSError, sqlite3.Error):
            continue
    return tuple(sorted(contacts))


__all__ = [
    "ClaimedLinkJob", "PrivateLinkResearchQueue", "discover_contacts_with_queues", "queue_path",
]
