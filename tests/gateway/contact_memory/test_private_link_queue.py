from __future__ import annotations

import os
from pathlib import Path
import pytest

from gateway.contact_memory.private_link_queue import PrivateLinkResearchQueue, queue_path


def test_queue_is_owner_only_physically_isolated_and_restart_reclaims(tmp_path: Path):
    first = PrivateLinkResearchQueue(tmp_path, "contact-a")
    second = PrivateLinkResearchQueue(tmp_path, "contact-b")
    assert first.path != second.path
    assert first.path == queue_path(tmp_path, "contact-a")
    assert first.path.stat().st_mode & 0o077 == 0
    assert first.path.parent.stat().st_mode & 0o077 == 0

    first.enqueue(
        job_id="a" * 64,
        event_id="b" * 64,
        url_id="c" * 64,
        exact_url="https://example.com/one",
        occurred_at=100.0,
        recency_bucket=3,
        engagement_score=0,
        repeated_shares=1,
        distinct_days=1,
    )
    assert second.pending_count() == 0
    claim = first.claim_next(now=1000.0, lease_seconds=10)
    assert claim is not None
    reopened = PrivateLinkResearchQueue(tmp_path, "contact-a")
    assert reopened.claim_next(now=1005.0, lease_seconds=10) is None
    reclaimed = reopened.claim_next(now=1011.0, lease_seconds=10)
    assert reclaimed is not None and reclaimed.job_id == claim.job_id


def test_queue_ranks_recency_then_engagement_but_eventually_services_old(tmp_path: Path):
    queue = PrivateLinkResearchQueue(tmp_path, "contact")
    jobs = [
        ("1" * 64, 0, 0, 1_000.0),
        ("2" * 64, 0, 3, 900.0),
        ("3" * 64, 3, 9, 100.0),
    ]
    for job_id, bucket, engagement, occurred in jobs:
        queue.enqueue(
            job_id=job_id,
            event_id=(job_id[0] * 64),
            url_id=((str((int(job_id[0]) + 3) % 10)) * 64),
            exact_url=f"https://example.com/{job_id[0]}",
            occurred_at=occurred,
            recency_bucket=bucket,
            engagement_score=engagement,
            repeated_shares=1,
            distinct_days=1,
        )
    first = queue.claim_next(now=2_000.0, lease_seconds=10, eventual_old_every=2)
    assert first is not None and first.job_id == "2" * 64
    queue.complete(first.job_id, first.claim_token, result_commitment="a" * 64, now=2_000.0)
    second = queue.claim_next(now=2_001.0, lease_seconds=10, eventual_old_every=2)
    assert second is not None and second.job_id == "3" * 64


def test_queue_retry_failure_codes_and_idempotency(tmp_path: Path):
    queue = PrivateLinkResearchQueue(tmp_path, "contact")
    event_id = "b" * 64

    def enqueue() -> bool:
        return queue.enqueue(
            job_id="a" * 64,
            event_id=event_id,
            url_id="c" * 64,
            exact_url="https://example.com/item",
            occurred_at=100.0,
            recency_bucket=1,
            engagement_score=1,
            repeated_shares=1,
            distinct_days=1,
        )

    assert enqueue()
    assert not enqueue()
    assert queue.engage_event(event_id, score=4, now=150.0) == 1
    assert not enqueue()
    claim = queue.claim_next(now=200.0, lease_seconds=10)
    assert claim is not None
    queue.retry(claim.job_id, claim.claim_token, failure_code="fetch_timeout", now=200.0)
    assert queue.claim_next(now=200.5, lease_seconds=10) is None
    retry = queue.claim_next(now=201.0, lease_seconds=10)
    assert retry is not None and retry.attempt_count == 2
    queue.complete(retry.job_id, retry.claim_token, result_commitment="d" * 64, now=202.0)
    assert queue.pending_count() == 0
    assert queue.stats()["complete"] == 1
    assert os.access(queue.path, os.R_OK | os.W_OK)


def test_complete_rejects_expired_lease_and_renewal_extends_it(tmp_path: Path) -> None:
    queue = PrivateLinkResearchQueue(tmp_path, "contact")
    queue.enqueue(
        job_id="a" * 64, event_id="b" * 64, url_id="c" * 64,
        exact_url="https://example.com/item", occurred_at=100.0,
        recency_bucket=0, engagement_score=0, repeated_shares=1, distinct_days=1,
    )
    claim = queue.claim_next(now=100.0, lease_seconds=10.0)
    assert claim is not None
    with pytest.raises(ValueError, match="stale"):
        queue.complete(
            claim.job_id, claim.claim_token, result_commitment="d" * 64, now=111.0,
        )
    renewed = queue.renew_claim(
        claim.job_id, claim.claim_token, now=105.0, lease_seconds=20.0,
    )
    assert renewed is not None
    queue.complete(
        claim.job_id, claim.claim_token, result_commitment="d" * 64, now=120.0,
    )
