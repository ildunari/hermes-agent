"""Asynchronous, pluggable post-turn contact-memory extraction.

Extractor output is untrusted.  This module validates it deterministically and
writes a review ledger before the store can promote a narrowly safe fact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import select
import subprocess
import threading
from typing import Any, Awaitable, Callable, Mapping, Protocol, cast

from .schema import AssertionType, Audience, FactProposal, FactStatus, MentionPolicy
from .store import ContactMemoryStore


class ExtractorBackend(Protocol):
    async def extract(self, user_text: str, assistant_text: str, metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]: ...


Extractor = Callable[[str, str, Mapping[str, Any]], Awaitable[list[Mapping[str, Any]]]]

_MAX_OPERATIONS = 12
_MAX_TEXT = 500
_MIN_CONFIDENCE = 0.60
_AUTO_CONFIDENCE = 0.95
_AUTO_TRUST = 0.90
_TOKEN_RE = re.compile(r"(?i)(?:api[_ -]?key|password|secret|private key|bearer\s+[a-z0-9._-]+|sk-[a-z0-9]{12,})")
_INSTRUCTION_RE = re.compile(r"(?i)(?:ignore (?:all |the )?(?:previous|prior)|system prompt|developer message|call (?:the )?tool|reveal (?:the )?secret|do not follow)")
_SENSITIVE_RE = re.compile(r"(?i)\b(?:diagnos|medication|pregnan|therapy|bank|debt|salary|income|tax|lawsuit|lawyer|visa|immigration|sex|sexual|fetish|relationship|break ?up|affair|abuse|arrest|crime|password|secret|token)\b")
_SAFE_PREDICATE_RE = re.compile(r"^(?:likes|dislikes|prefers|favorite_|uses_|owns_|lives_in$|works_at$|has_hobby$|has_pet$)")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:_./-]{0,127}$", re.I)
QWEN3_EXTRACTOR_MODEL = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
QWEN3_EXTRACTOR_REVISION = "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
QWEN3_EXTRACTOR_VENV = Path.home() / ".cache/hermes-contact-memory/embeddinggemma-venv"


class ExtractorWorkerError(RuntimeError):
    """The isolated local extractor could not serve a request."""


class Qwen3ExtractorBackend:
    """Persistent JSON-lines client for the isolated MLX Qwen3 extractor."""

    def __init__(
        self,
        model_name: str = QWEN3_EXTRACTOR_MODEL,
        *,
        revision: str = QWEN3_EXTRACTOR_REVISION,
        python_path: str | Path | None = None,
        timeout_seconds: float = 15.0,
        startup_timeout_seconds: float = 180.0,
        max_tokens: int = 320,
    ) -> None:
        self.model_id = str(model_name)
        self.revision = str(revision)
        self.python_path = Path(python_path or QWEN3_EXTRACTOR_VENV / "bin/python").expanduser()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.max_tokens = max(32, min(int(max_tokens), 1024))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _readline(proc: subprocess.Popen[str], timeout: float) -> str:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError
        return proc.stdout.readline()

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.python_path.is_file():
            raise ExtractorWorkerError(f"extractor environment not installed: {self.python_path}")
        worker = Path(__file__).with_name("qwen3_extractor_worker.py")
        env = os.environ.copy()
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        command = [
            str(self.python_path), "-u", str(worker), "--model", self.model_id,
            "--revision", self.revision, "--max-tokens", str(self.max_tokens),
        ]
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        try:
            line = self._readline(self._process, self.startup_timeout_seconds)
            payload = json.loads(line) if line else {}
            if payload.get("ready") is not True:
                raise ValueError("invalid readiness response")
        except (TimeoutError, json.JSONDecodeError, ValueError) as exc:
            detail = ""
            if self._process.poll() is not None and self._process.stderr:
                detail = self._process.stderr.read().strip()[-1000:]
            self.close()
            raise ExtractorWorkerError(f"extractor worker failed to start: {detail}".rstrip()) from exc
        return self._process

    def _extract_sync(
        self, user_text: str, assistant_text: str, metadata: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        del assistant_text  # Assistant claims are deliberately outside extraction evidence.
        source_id = str(metadata.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("source_id is required")
        with self._lock:
            proc = self._start()
            assert proc.stdin is not None
            try:
                proc.stdin.write(json.dumps({
                    "user_text": str(user_text)[:4000], "source_id": source_id,
                }, separators=(",", ":")) + "\n")
                proc.stdin.flush()
                line = self._readline(proc, self.timeout_seconds)
                if not line:
                    raise OSError("worker exited")
                payload = json.loads(line)
                proposals = payload.get("proposals")
                if not payload.get("ok") or not isinstance(proposals, list):
                    raise ExtractorWorkerError(str(payload.get("error") or "invalid extractor response"))
                return proposals
            except TimeoutError as exc:
                self.close()
                raise ExtractorWorkerError("extractor worker timed out") from exc
            except (BrokenPipeError, json.JSONDecodeError, OSError) as exc:
                self.close()
                raise ExtractorWorkerError(f"extractor worker request failed: {exc}") from exc

    async def extract(
        self, user_text: str, assistant_text: str, metadata: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        return await asyncio.to_thread(
            self._extract_sync, user_text, assistant_text, metadata
        )

    def close(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def __del__(self) -> None:  # pragma: no cover - best effort at interpreter exit
        try:
            self.close()
        except Exception:
            pass


def extractor_from_config(config: object) -> ExtractorBackend | None:
    if not isinstance(config, dict) or not config.get("extraction"):
        return None
    raw = config.get("extractor")
    if isinstance(raw, str):
        raw = {"backend": raw}
    if not isinstance(raw, dict):
        raw = {}
    name = str(raw.get("backend") or "qwen3-mlx").strip().lower()
    if name in {"off", "none"}:
        return None
    if name not in {"qwen3", "qwen3-mlx", "qwen3-extractor"}:
        raise ValueError(f"unknown contact-memory extractor backend: {name}")
    return Qwen3ExtractorBackend(
        str(raw.get("model") or QWEN3_EXTRACTOR_MODEL),
        revision=str(raw.get("revision") or QWEN3_EXTRACTOR_REVISION),
        python_path=raw.get("python_path"),
        timeout_seconds=float(raw.get("timeout_seconds") or 15),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds") or 180),
        max_tokens=int(raw.get("max_tokens") or 320),
    )


def _clean(value: Any, field: str, *, limit: int = _MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        raise ValueError(f"invalid {field}")
    return text


def normalized_fact_key(proposal: FactProposal) -> str:
    """Stable content+evidence identity independent of model formatting/case."""
    parts = (
        proposal.subject_id.casefold().strip(), proposal.predicate.casefold().strip(),
        " ".join(proposal.object_text.casefold().split()), proposal.evidence_pointer.casefold().strip(),
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def validate_pending_operation(raw: Mapping[str, Any], contact_id: str, source_id: str) -> FactProposal:
    allowed = {"logical_id", "subject_id", "predicate", "object_text", "audience", "mention_policy", "assertion_type", "trust", "confidence", "evidence_pointer", "metadata"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown proposal fields: {sorted(unknown)}")
    logical_id = _clean(raw.get("logical_id"), "logical_id", limit=128)
    predicate = _clean(raw.get("predicate"), "predicate", limit=128)
    if not _ID_RE.fullmatch(logical_id) or not _ID_RE.fullmatch(predicate):
        raise ValueError("logical_id and predicate must be normalized identifiers")
    subject = _clean(raw.get("subject_id"), "subject_id", limit=128)
    # Extraction is contact-first-party only. Third-party and ambiguous pronoun
    # assignment is rejected rather than guessed into a private namespace.
    if subject != "person:contact":
        raise ValueError("subject must be the authenticated contact")
    text = _clean(raw.get("object_text"), "object_text")
    if _TOKEN_RE.search(text):
        raise ValueError("secret-like content is not memory")
    if _INSTRUCTION_RE.search(text):
        raise ValueError("instruction-like content is not memory")
    # Evidence provenance is supplied by authenticated routing, never by the model.
    evidence = _clean(source_id, "evidence_pointer", limit=200)
    confidence = float(raw.get("confidence", _MIN_CONFIDENCE))
    trust = float(raw.get("trust", 0.0))
    if confidence < _MIN_CONFIDENCE:
        raise ValueError("proposal confidence is below threshold")
    metadata = dict(raw.get("metadata") or {})
    if len(json.dumps(metadata, ensure_ascii=False)) > 2000:
        raise ValueError("proposal metadata is too large")
    # Parse model-supplied enum values even though policy narrows audience.
    Audience(str(raw.get("audience") or "owner_review"))
    return FactProposal(
        logical_id=logical_id, subject_id=subject, predicate=predicate,
        object_text=text,
        # A model can only make this more conservative; it can never grant guest access.
        audience=Audience.OWNER_REVIEW,
        mention_policy=MentionPolicy(str(raw.get("mention_policy") or "background")),
        assertion_type=AssertionType(str(raw.get("assertion_type") or "inferred")),
        source_id=source_id, source_contact_id=contact_id,
        evidence_pointer=evidence, trust=trust, confidence=confidence,
        status=FactStatus.PENDING, metadata=metadata,
    )


def is_safe_to_auto_promote(proposal: FactProposal) -> bool:
    """Intentionally narrow policy; uncertainty always falls back to review."""
    category = str(proposal.metadata.get("category") or "").casefold()
    sensitivity = str(proposal.metadata.get("sensitivity") or "normal").casefold()
    return (
        proposal.subject_id == "person:contact"
        and proposal.assertion_type is AssertionType.STATED
        and proposal.mention_policy is MentionPolicy.BACKGROUND
        and proposal.confidence >= _AUTO_CONFIDENCE
        and proposal.trust >= _AUTO_TRUST
        and sensitivity == "normal"
        and category in {"preference", "possession", "location", "work", "hobby", "pet"}
        and bool(_SAFE_PREDICATE_RE.match(proposal.predicate))
        and not _SENSITIVE_RE.search(" ".join((proposal.predicate, proposal.object_text)))
    )


async def propose_turn_memories(store: ContactMemoryStore, extractor: Extractor | ExtractorBackend, user_text: str, assistant_text: str, metadata: Mapping[str, Any]) -> list[str]:
    source_id = str(metadata.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("trusted source_id is required")
    method = cast(Extractor, getattr(extractor, "extract", extractor))
    raw_operations = await method(user_text, assistant_text, metadata)
    if not isinstance(raw_operations, list) or len(raw_operations) > _MAX_OPERATIONS:
        raise ValueError("extractor returned an invalid operation batch")
    proposal_ids: list[str] = []
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise ValueError("extractor operation must be an object")
        if raw.get("kind") == "recommendation":
            allowed = {"kind", "topic", "recommendation", "basis_fact_ids", "confidence", "change_requirements", "expires_at"}
            if set(raw) - allowed:
                raise ValueError("unknown recommendation fields")
            topic = _clean(raw.get("topic"), "topic", limit=128)
            recommendation = _clean(raw.get("recommendation"), "recommendation")
            confidence = float(raw.get("confidence", 0.0))
            basis = raw.get("basis_fact_ids")
            requirements = raw.get("change_requirements")
            if confidence < _MIN_CONFIDENCE or not isinstance(basis, list) or not basis:
                raise ValueError("recommendation requires confidence and basis facts")
            if not isinstance(requirements, list) or not requirements:
                raise ValueError("recommendation requires change requirements")
            basis_ids = [_clean(item, "basis_fact_id", limit=64) for item in basis[:12]]
            changes = [_clean(item, "change_requirement", limit=200) for item in requirements[:12]]
            stable = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            rec_id = await asyncio.to_thread(
                store.set_recommendation, topic, recommendation, basis_ids,
                confidence=confidence,
                status="active" if confidence >= _AUTO_CONFIDENCE else "proposed",
                change_requirements=changes,
                expires_at=float(raw["expires_at"]) if raw.get("expires_at") is not None else None,
                idempotency_key=hashlib.sha256(f"{source_id}\0{stable}".encode()).hexdigest(),
            )
            proposal_ids.append(rec_id)
            continue
        proposal = validate_pending_operation(raw, store.contact_id, source_id)
        result = await asyncio.to_thread(
            store.ingest_extracted_fact, proposal,
            idempotency_key=normalized_fact_key(proposal),
            auto_promote=is_safe_to_auto_promote(proposal),
        )
        proposal_ids.append(str(result["proposal_id"]))
    return proposal_ids


@dataclass(frozen=True)
class ExtractionJob:
    store: ContactMemoryStore
    user_text: str
    assistant_text: str
    metadata: Mapping[str, Any]


class PostTurnExtractionRuntime:
    """Bounded fail-open queue; submitting a completed turn never awaits ML."""

    def __init__(self, backend: ExtractorBackend, *, max_queue: int = 64, workers: int = 1, max_retries: int = 1):
        if max_queue < 1 or workers < 1 or max_retries < 0:
            raise ValueError("queue bounds, worker count, and retries must be valid")
        self.backend = backend
        self.queue: asyncio.Queue[ExtractionJob | None] = asyncio.Queue(maxsize=max_queue)
        self.worker_count = workers
        self.max_retries = int(max_retries)
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self.failures = 0
        self.dropped = 0

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]

    def submit(self, job: ExtractionJob) -> bool:
        if self._closing:
            self.dropped += 1
            return False
        self.start()
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:
                    return
                for attempt in range(self.max_retries + 1):
                    try:
                        await propose_turn_memories(job.store, self.backend, job.user_text, job.assistant_text, job.metadata)
                        break
                    except Exception:
                        if attempt >= self.max_retries:
                            # Extraction is auxiliary: malformed model output or
                            # a dead worker cannot affect the produced reply.
                            self.failures += 1
                        else:
                            await asyncio.sleep(0)
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        self._closing = True
        if self._tasks:
            await self.queue.join()
            for _ in self._tasks:
                await self.queue.put(None)
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        await self._close_backend()

    async def force_close(self) -> None:
        """Cancel workers and release resources after a bounded drain fails."""
        self._closing = True
        tasks, self._tasks = list(self._tasks), []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()
        await self._close_backend()

    async def _close_backend(self) -> None:
        close = getattr(self.backend, "close", None)
        if not callable(close):
            return
        if asyncio.iscoroutinefunction(close):
            await close()
        else:
            value = await asyncio.to_thread(close)
            if inspect.isawaitable(value):
                await value
