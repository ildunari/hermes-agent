import hashlib
import os
from pathlib import Path
import time

import pytest

from hermes_cli.browser_upload_sources import (
    BrowserUploadSourceAuthority,
    BrowserUploadSourceError,
    BrowserUploadSourceScope,
)


@pytest.fixture
def scope():
    return BrowserUploadSourceScope(
        recipient="dashboard:user-1",
        profile="coding",
        connection_id="connection-1",
        transport_id="transport-1",
        browser_sid="browser-sid-1",
        capability_generation="capability-1",
        task_id="task-1",
        task_generation="1",
        tab_id="browser:tab-1",
        tab_incarnation="tab-incarnation-1",
        binding_generation="binding-1",
        document_generation="document-1",
        frame_id="frame-1",
        origin="https://example.test",
        chooser_id="chooser-1",
        backend_node_id="node-1",
        form_fingerprint="form-1",
        chooser_mode="selectSingle",
        source_session_id="session-1",
    )


@pytest.fixture
def source_authority(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sequence = iter(f"{index:032d}" for index in range(1000))
    authority = BrowserUploadSourceAuthority(
        workspace_root=lambda profile, session: (
            workspace if (profile, session) == ("coding", "session-1") else None
        ),
        token=lambda _bytes: next(sequence),
    )
    return authority, workspace


def _candidate(authority, scope, name):
    candidates = authority.list_candidates(scope)
    return next(candidate for candidate in candidates if candidate.display_name == name)


def test_candidate_is_opaque_and_one_use_stream_is_exact(source_authority, scope):
    authority, workspace = source_authority
    payload = b"exact source bytes"
    path = workspace / "report.txt"
    path.write_bytes(payload)

    candidate = _candidate(authority, scope, path.name)
    assert candidate.display_name == "report.txt"
    assert str(path) not in candidate.candidate_id

    ticket = authority.mint(scope, candidate.candidate_id)
    assert ticket.ref != ticket.credential
    assert ticket.size == len(payload)
    assert ticket.sha256 == hashlib.sha256(payload).hexdigest()
    assert str(path) not in ticket.ref + ticket.credential

    read = authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)
    assert b"".join(read.chunks(chunk_bytes=3)) == payload
    with pytest.raises(BrowserUploadSourceError, match="grant_unavailable"):
        authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)


def test_candidates_reject_symlink_hardlink_fifo_sparse_and_oversize(source_authority, scope):
    authority, workspace = source_authority
    safe = workspace / "safe.txt"
    safe.write_text("safe")
    (workspace / "link.txt").symlink_to(safe)
    os.link(safe, workspace / "hard.txt")
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    sparse = workspace / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.seek(32 * 1024 * 1024)
        handle.write(b"x")
    oversize = workspace / "oversize.bin"
    with oversize.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)

    assert authority.list_candidates(scope) == []


def test_candidate_swap_and_mutation_fail_closed(source_authority, scope):
    authority, workspace = source_authority
    source = workspace / "source.txt"
    source.write_text("first")
    candidate = _candidate(authority, scope, source.name)
    replacement = workspace / "replacement.txt"
    replacement.write_text("other")
    replacement.replace(source)

    with pytest.raises(BrowserUploadSourceError, match="UPLOAD_SOURCE_MUTATED"):
        authority.mint(scope, candidate.candidate_id)

    source.write_text("original")
    candidate = _candidate(authority, scope, source.name)
    ticket = authority.mint(scope, candidate.candidate_id)
    source.write_text("changed-after-hash")
    with pytest.raises(BrowserUploadSourceError, match="UPLOAD_SOURCE_MUTATED"):
        authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)


def test_exact_tuple_mismatch_revokes_held_descriptor(source_authority, scope):
    authority, workspace = source_authority
    (workspace / "one.txt").write_text("one")
    candidate = _candidate(authority, scope, "one.txt")
    ticket = authority.mint(scope, candidate.candidate_id)
    wrong = BrowserUploadSourceScope(**{**scope.__dict__, "chooser_id": "chooser-2"})

    with pytest.raises(BrowserUploadSourceError, match="grant_scope_mismatch"):
        authority.claim(ticket.ref, ticket.credential, wrong, ticket.source_record_revision)
    with pytest.raises(BrowserUploadSourceError, match="grant_unavailable"):
        authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)


@pytest.mark.parametrize("revision", [None, "9" * 32])
def test_authenticated_revision_mismatch_consumes_grant(source_authority, scope, revision):
    authority, workspace = source_authority
    (workspace / "revision.txt").write_text("revision")
    candidate = _candidate(authority, scope, "revision.txt")
    ticket = authority.mint(scope, candidate.candidate_id)

    with pytest.raises(BrowserUploadSourceError, match="grant_scope_mismatch"):
        authority.claim(ticket.ref, ticket.credential, scope, revision)
    with pytest.raises(BrowserUploadSourceError, match="grant_unavailable"):
        authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)


def test_expiry_closes_unclaimed_grants(scope, tmp_path):
    clock = [10.0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("one")
    sequence = iter(f"{index:032d}" for index in range(100))
    authority = BrowserUploadSourceAuthority(
        workspace_root=lambda _profile, _session: workspace,
        clock=lambda: clock[0],
        token=lambda _bytes: next(sequence),
    )
    candidate = _candidate(authority, scope, "one.txt")
    ticket = authority.mint(scope, candidate.candidate_id, ttl_seconds=2)
    clock[0] += 2

    with pytest.raises(BrowserUploadSourceError, match="grant_unavailable"):
        authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)
    assert authority.revoke_all() == 0


def test_active_transfer_has_a_hard_five_minute_deadline(scope, tmp_path):
    clock = [10.0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "two.txt").write_text("two")
    sequence = iter(f"{index:032d}" for index in range(100))
    authority = BrowserUploadSourceAuthority(
        workspace_root=lambda _profile, _session: workspace,
        clock=lambda: clock[0],
        token=lambda _bytes: next(sequence),
    )
    candidate = _candidate(authority, scope, "two.txt")
    ticket = authority.mint(scope, candidate.candidate_id)
    read = authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)
    clock[0] += 5 * 60

    with pytest.raises(BrowserUploadSourceError, match="UPLOAD_EXPIRED"):
        next(read.chunks(chunk_bytes=1))


def test_one_active_stream_per_chooser_and_two_per_connection(source_authority, scope):
    authority, workspace = source_authority
    for name in ("one.txt", "two.txt", "three.txt"):
        (workspace / name).write_text(name)
    candidates = {candidate.display_name: candidate for candidate in authority.list_candidates(scope)}
    tickets = [authority.mint(scope, candidates[name].candidate_id) for name in candidates]

    first = authority.claim(
        tickets[0].ref, tickets[0].credential, scope, tickets[0].source_record_revision
    )
    with pytest.raises(BrowserUploadSourceError, match="grant_capacity"):
        authority.claim(
            tickets[1].ref, tickets[1].credential, scope, tickets[1].source_record_revision
        )
    first.close()
    second = authority.claim(
        tickets[1].ref, tickets[1].credential, scope, tickets[1].source_record_revision
    )
    second.close()


def test_lifecycle_revocation_aborts_an_active_descriptor(source_authority, scope):
    authority, workspace = source_authority
    (workspace / "active.txt").write_text("active")
    candidate = _candidate(authority, scope, "active.txt")
    ticket = authority.mint(scope, candidate.candidate_id)
    read = authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)

    assert authority.revoke_where(connection_id=scope.connection_id) == 1
    with pytest.raises((BrowserUploadSourceError, OSError)):
        next(read.chunks())
    read.close()


def test_revoked_stream_cannot_read_from_reused_descriptor(source_authority, scope, tmp_path):
    authority, workspace = source_authority
    (workspace / "active.txt").write_bytes(b"AB")
    ticket = authority.mint(scope, _candidate(authority, scope, "active.txt").candidate_id)
    read = authority.claim(ticket.ref, ticket.credential, scope, ticket.source_record_revision)
    chunks = read.chunks(chunk_bytes=1)
    assert next(chunks) == b"A"
    descriptor = read._source.fd

    assert authority.revoke_where(connection_id=scope.connection_id) == 1
    foreign = tmp_path / "foreign-secret.txt"
    foreign.write_bytes(b"FOREIGN")
    foreign_fd = os.open(foreign, os.O_RDONLY)
    if foreign_fd != descriptor:
        os.dup2(foreign_fd, descriptor)
        os.close(foreign_fd)
        foreign_fd = descriptor
    try:
        with pytest.raises(BrowserUploadSourceError, match="UPLOAD_EXPIRED"):
            next(chunks)
    finally:
        os.close(foreign_fd)


def test_stalled_active_transfer_timer_releases_chooser_slot(scope, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("one")
    (workspace / "two.txt").write_text("two")
    authority = BrowserUploadSourceAuthority(
        workspace_root=lambda _profile, _session: workspace,
        transfer_seconds=0.03,
    )
    candidates = {row.display_name: row for row in authority.list_candidates(scope)}
    first_ticket = authority.mint(scope, candidates["one.txt"].candidate_id)
    second_ticket = authority.mint(scope, candidates["two.txt"].candidate_id)
    stalled = authority.claim(
        first_ticket.ref,
        first_ticket.credential,
        scope,
        first_ticket.source_record_revision,
    )
    with pytest.raises(BrowserUploadSourceError, match="grant_capacity"):
        authority.claim(
            second_ticket.ref,
            second_ticket.credential,
            scope,
            second_ticket.source_record_revision,
        )

    deadline = time.monotonic() + 1
    while True:
        try:
            replacement = authority.claim(
                second_ticket.ref,
                second_ticket.credential,
                scope,
                second_ticket.source_record_revision,
            )
            break
        except BrowserUploadSourceError as exc:
            assert exc.code == "grant_capacity"
            assert time.monotonic() < deadline
            time.sleep(0.01)
    replacement.close()
    with pytest.raises(BrowserUploadSourceError, match="UPLOAD_EXPIRED"):
        next(stalled.chunks())


def test_nested_symlink_component_never_becomes_a_candidate(source_authority, scope, tmp_path):
    authority, workspace = source_authority
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (workspace / "linked-directory").symlink_to(outside, target_is_directory=True)
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "allowed.txt").write_text("allowed")

    candidates = authority.list_candidates(scope)

    assert [candidate.display_name for candidate in candidates] == ["allowed.txt"]
