from pathlib import Path

import pytest

from hermes_cli.browser_resource_grants import (
    BrowserGrantError,
    BrowserGrantScope,
    BrowserResourceGrantAuthority,
)


@pytest.fixture
def scope():
    return BrowserGrantScope(
        recipient="dashboard:user-1",
        profile="coding",
        connection_id="desktop-connection-1",
        tab_id="browser:tab-1",
        guest_generation="guest-generation-1",
        source_session_id="session-1",
    )


@pytest.fixture
def authority(tmp_path):
    roots = {("coding", "session-1"): tmp_path / "workspace"}
    roots[("coding", "session-1")].mkdir()
    sequence = iter(f"{index:032d}" for index in range(100))
    return BrowserResourceGrantAuthority(
        workspace_root=lambda profile, session: roots.get((profile, session)),
        token=lambda _bytes: next(sequence),
    )


def test_artifact_grant_uses_separate_opaque_ref_and_credential_and_ranges(authority, scope, tmp_path):
    artifact = tmp_path / "workspace" / "report.pdf"
    artifact.write_bytes(b"0123456789")

    grant = authority.mint_artifact(scope, str(artifact))

    assert grant.ref != grant.credential
    assert str(artifact) not in grant.ref
    assert str(artifact) not in grant.credential
    assert grant.display_name == "report.pdf"
    assert grant.mime_type == "application/pdf"
    partial = authority.authorize_artifact(grant.ref, grant.credential, scope, "bytes=2-5")
    assert (partial.start, partial.end, partial.status_code) == (2, 5, 206)
    assert partial.path.read_bytes()[partial.start : partial.end + 1] == b"2345"


@pytest.mark.parametrize("candidate", ["../outside.pdf", "nested/../../outside.pdf"])
def test_artifact_grant_rejects_traversal_outside_session_workspace(authority, scope, tmp_path, candidate):
    (tmp_path / "outside.pdf").write_bytes(b"outside")

    with pytest.raises(BrowserGrantError, match="artifact_out_of_scope"):
        authority.mint_artifact(scope, candidate)


def test_artifact_grant_rejects_symlink_escape_sensitive_files_and_post_mint_mutation(authority, scope, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (workspace / "escape.pdf").symlink_to(outside)
    (workspace / ".env").write_text("TOKEN=secret")
    mutable = workspace / "mutable.html"
    mutable.write_text("first")

    with pytest.raises(BrowserGrantError, match="artifact_out_of_scope"):
        authority.mint_artifact(scope, "escape.pdf")
    with pytest.raises(BrowserGrantError, match="artifact_sensitive"):
        authority.mint_artifact(scope, ".env")

    grant = authority.mint_artifact(scope, "mutable.html")
    replacement = workspace / "replacement.html"
    replacement.write_text("other")
    replacement.replace(mutable)
    with pytest.raises(BrowserGrantError, match="artifact_changed"):
        authority.authorize_artifact(grant.ref, grant.credential, scope)
    with pytest.raises(BrowserGrantError, match="grant_unavailable"):
        authority.authorize_artifact(grant.ref, grant.credential, scope)


def test_valid_credential_with_cross_tab_scope_revokes_instead_of_retargeting(authority, scope, tmp_path):
    (tmp_path / "workspace" / "page.html").write_text("<h1>safe</h1>")
    grant = authority.mint_artifact(scope, "page.html")
    wrong_tab = BrowserGrantScope(**{**scope.__dict__, "tab_id": "browser:tab-2"})

    with pytest.raises(BrowserGrantError, match="grant_scope_mismatch"):
        authority.authorize_artifact(grant.ref, grant.credential, wrong_tab)
    with pytest.raises(BrowserGrantError, match="grant_unavailable"):
        authority.authorize_artifact(grant.ref, grant.credential, scope)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test:4173/",
        "http://localhost:4173/",
        "http://0.0.0.0:4173/",
        "http://192.168.1.2:4173/",
        "http://127.0.0.1/",
        "ftp://127.0.0.1:21/",
        "http://user:password@127.0.0.1:4173/",
        "http://127.0.0.1:4173/?access_token=secret",
    ],
)
def test_preview_grant_rejects_hostnames_non_loopback_implicit_ports_credentials_and_secrets(authority, scope, url):
    with pytest.raises(BrowserGrantError):
        authority.mint_preview(scope, url)


@pytest.mark.parametrize(
    "url,expected_host",
    [
        ("http://127.0.0.1:4173/app/index.html", "127.0.0.1"),
        ("http://127.99.1.2:4173/app/index.html", "127.99.1.2"),
        ("http://[::1]:4173/app/index.html", "::1"),
        ("http://[::ffff:127.0.0.1]:4173/app/index.html", "::ffff:127.0.0.1"),
    ],
)
def test_preview_grant_binds_exact_literal_loopback_origin(authority, scope, url, expected_host):
    grant = authority.mint_preview(scope, url)

    assert grant.host == expected_host
    assert grant.port == 4173
    assert grant.origin.endswith(":4173")
    assert authority.preview_target(grant, "app/main.js", "") == f"{grant.origin}/app/main.js"


def test_preview_redirects_stay_inside_exact_origin_and_are_rewritten_to_opaque_route(authority, scope):
    grant = authority.mint_preview(scope, "http://127.0.0.1:4173/app/index.html")
    current = authority.preview_target(grant, "app/index.html", "")

    rewritten = authority.rewrite_preview_redirect(grant, current, "../login?next=%2Fapp")
    assert rewritten == f"/api/browser/preview/{grant.ref}/login?next=%2Fapp"
    assert grant.origin not in rewritten

    for location in [
        "http://127.0.0.1:4174/port-change",
        "http://127.0.0.2:4173/host-change",
        "https://127.0.0.1:4173/scheme-change",
        "//localhost:4173/hostname",
        "/login?token=secret",
    ]:
        with pytest.raises(BrowserGrantError, match="preview_redirect_denied"):
            authority.rewrite_preview_redirect(grant, current, location)


def test_expiry_and_lifecycle_revocation_are_memory_only(scope, tmp_path):
    clock = [100.0]
    sequence = iter(f"{index:032d}" for index in range(20))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.html").write_text("one")
    authority = BrowserResourceGrantAuthority(
        workspace_root=lambda _profile, _session: workspace,
        clock=lambda: clock[0],
        token=lambda _bytes: next(sequence),
    )
    artifact = authority.mint_artifact(scope, "one.html", ttl_seconds=5)
    preview = authority.mint_preview(scope, "http://127.0.0.1:4173/", ttl_seconds=5)

    assert authority.revoke_where(tab_id=scope.tab_id, guest_generation=scope.guest_generation) == 2
    assert authority.revoke_where(profile=scope.profile) == 0

    expiring = authority.mint_preview(scope, "http://127.0.0.1:4173/", ttl_seconds=5)
    clock[0] += 5
    with pytest.raises(BrowserGrantError, match="grant_unavailable"):
        authority.authorize_preview(expiring.ref, expiring.credential, scope)
    assert authority.revoke_all() == 0


def test_range_parser_fails_closed_for_multi_range_and_out_of_bounds(authority, scope, tmp_path):
    artifact = tmp_path / "workspace" / "range.pdf"
    artifact.write_bytes(b"0123456789")
    grant = authority.mint_artifact(scope, str(artifact))

    for value in ["bytes=0-1,4-5", "items=0-1", "bytes=20-30", "bytes=-0"]:
        with pytest.raises(BrowserGrantError, match="range_invalid"):
            authority.authorize_artifact(grant.ref, grant.credential, scope, value)


def test_empty_artifact_has_zero_length_full_response(authority, scope, tmp_path):
    artifact = tmp_path / "workspace" / "empty.pdf"
    artifact.write_bytes(b"")
    grant = authority.mint_artifact(scope, str(artifact))

    read = authority.authorize_artifact(grant.ref, grant.credential, scope)

    assert read.status_code == 200
    assert read.end - read.start + 1 == 0
