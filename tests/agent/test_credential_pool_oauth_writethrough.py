"""Regression tests for credential-pool OAuth refresh write-through to root.

Companion to ``tests/hermes_cli/test_xai_oauth_writethrough.py``. That file
covers the *non-pool* xAI refresh path (``_save_xai_oauth_tokens``). These
cover the **credential-pool** refresh path
(``CredentialPool._sync_device_code_entry_to_auth_store``): when a profile
that has no own ``providers.<id>`` block refreshes — via the pool — a rotating
OAuth grant it resolved from the global-root fallback, the rotated chain must
be written back to the global root too. Otherwise root keeps a revoked refresh
token and every other profile reading root's stale grant dies with
``refresh_token_reused`` / ``invalid_grant`` once its access token expires
(issue #48415, the Codex/xAI analog of #43589).

The tests drive the real ``_sync_device_code_entry_to_auth_store`` against
real on-disk auth stores (profile + root under ``tmp_path``) rather than
mocking the save boundary, so they exercise the actual atomic write path.
"""

import json
import threading

import pytest

from agent import credential_pool as CP
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CredentialPool,
    PooledCredential,
)
from hermes_cli import auth as A


def _write_store(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def _read_store(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(provider: str, *, id: str, access_token: str, refresh_token: str):
    return PooledCredential(
        provider=provider,
        id=id,
        label="cred",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="device_code",
        access_token=access_token,
        refresh_token=refresh_token,
    )


@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    """Wire a profile auth store + a distinct global-root auth store on disk.

    The pytest seat belt in ``_write_through_provider_state_to_global_root``
    only refuses the *real* user's ``$HOME/.hermes/auth.json``; a tmp_path
    root is allowed, so point HOME away from the tmp root to keep the guard
    from tripping on these fixtures.
    """
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"

    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    return profile_path, root_path








def test_global_write_through_preserves_concurrent_root_update(
    profile_and_root, monkeypatch
):
    """A stale profile write-through must not erase a concurrent root login."""
    _profile_path, root_path = profile_and_root
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {"access_token": "old-xai", "refresh_token": "old-r"}
                }
            },
            "credential_pool": {
                "anthropic": [{"id": "anthropic-existing"}],
                "openrouter": [{"id": "openrouter-existing"}],
            },
        },
    )

    helper_loaded = threading.Event()
    helper_has_target_lock = threading.Event()
    allow_helper_save = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    real_auth_load = A._load_auth_store

    def paused_helper_load(path=None):
        store = real_auth_load(path)
        if threading.current_thread().name == "profile-write-through":
            target_holder = A._auth_lock_holder_for(root_path)
            if getattr(target_holder, "depth", 0) > 0:
                helper_has_target_lock.set()
            helper_loaded.set()
            assert allow_helper_save.wait(timeout=5)
        return store

    monkeypatch.setattr(A, "_load_auth_store", paused_helper_load)
    # The pre-fix implementation imported the loader directly; patch both
    # bindings so reverting the safe helper still exercises the stale ordering.
    monkeypatch.setattr(CP, "_load_auth_store", paused_helper_load)

    def profile_write_through():
        CP._write_through_provider_state_to_global_root(
            "xai-oauth",
            {"tokens": {"access_token": "new-xai", "refresh_token": "new-r"}},
        )

    def concurrent_codex_login():
        writer_started.set()
        with A._auth_store_lock(target_path=root_path):
            store = A._load_auth_store(root_path)
            A._store_provider_state(
                store,
                "openai-codex",
                {"tokens": {"access_token": "codex-a", "refresh_token": "codex-r"}},
                set_active=False,
            )
            pool = store.setdefault("credential_pool", {})
            pool["openai-codex"] = [{"id": "codex-login"}]
            A._save_auth_store(store, target_path=root_path)
        writer_done.set()

    helper = threading.Thread(target=profile_write_through, name="profile-write-through")
    helper.start()
    assert helper_loaded.wait(timeout=5)

    writer = threading.Thread(target=concurrent_codex_login, name="concurrent-login")
    writer.start()
    assert writer_started.wait(timeout=5)
    # A fixed helper already owns the target lock, so the writer will merge
    # after release. A reverted unlocked helper must first let the competing
    # login finish; only then do we release its stale save. This makes the
    # losing pre-fix ordering deterministic rather than scheduler-dependent.
    if not helper_has_target_lock.is_set():
        assert writer_done.wait(timeout=5)
    allow_helper_save.set()
    helper.join(timeout=5)
    writer.join(timeout=5)
    assert not helper.is_alive()
    assert not writer.is_alive()

    root = _read_store(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "new-r"
    assert root["providers"]["openai-codex"]["tokens"]["refresh_token"] == "codex-r"
    assert root["credential_pool"]["openai-codex"] == [{"id": "codex-login"}]
    assert root["credential_pool"]["anthropic"] == [{"id": "anthropic-existing"}]
    assert root["credential_pool"]["openrouter"] == [{"id": "openrouter-existing"}]


def test_codex_pool_refresh_holds_auth_store_lock_across_post(monkeypatch, tmp_path):
    """The Codex OAuth pool refresh must POST under the cross-process auth lock.

    Codex refresh tokens are single-use. If two Hermes processes both read the
    same on-disk token and both POST it, the loser gets ``refresh_token_reused``.
    Serializing the sync -> refresh POST -> write-back sequence through the
    shared ``_auth_store_lock`` closes that window: a second process blocks on
    the flock and, once inside, adopts the rotated token instead of re-POSTing.

    This asserts the invariant directly — that ``refresh_codex_oauth_pure`` is
    only ever called while the auth-store lock is held — rather than snapshotting
    any token value.
    """
    provider = "openai-codex"
    profile_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))

    lock_held: dict = {"during_post": None}
    real_lock = A._auth_store_lock

    depth = {"n": 0}

    import contextlib

    @contextlib.contextmanager
    def tracking_lock(*args, **kwargs):
        depth["n"] += 1
        try:
            with real_lock(*args, **kwargs):
                yield
        finally:
            depth["n"] -= 1

    monkeypatch.setattr(A, "_auth_store_lock", tracking_lock)
    # credential_pool imported _auth_store_lock by name; patch that binding too.
    monkeypatch.setattr(CP, "_auth_store_lock", tracking_lock)

    def fake_refresh(access_token, refresh_token, **kwargs):
        # The POST to the token endpoint must happen with the lock held.
        lock_held["during_post"] = depth["n"] > 0
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "last_refresh": "2020-01-02T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    entry = _entry(
        provider,
        id="codex-1",
        access_token="stale-access",
        refresh_token="stale-refresh",
    )
    pool = CredentialPool(provider, [entry])

    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert refreshed.access_token == "rotated-access"
    assert refreshed.refresh_token == "rotated-refresh"
    # The invariant: the single-use token POST ran inside the auth-store lock.
    assert lock_held["during_post"] is True


def test_inherited_codex_pool_refresh_stays_owned_by_root(
    profile_and_root, monkeypatch
):
    """Refreshing a root-fallback Codex row must not clone it into the profile."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "shared-codex",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(
        A,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "last_refresh": "2026-07-15T00:00:00Z",
        },
    )

    pool = CP.load_pool("openai-codex")
    entry = pool.entries()[0]
    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert refreshed.refresh_token == "new-refresh"
    profile = _read_store(profile_path)
    assert not profile.get("credential_pool", {}).get("openai-codex")
    assert not profile.get("providers", {}).get("openai-codex")
    root = _read_store(root_path)
    root_entry = root["credential_pool"]["openai-codex"][0]
    assert root_entry["access_token"] == "new-access"
    assert root_entry["refresh_token"] == "new-refresh"


def test_inherited_codex_pool_rereads_root_after_lock_wait(
    profile_and_root, monkeypatch
):
    """A waiter adopts the winner's rotated root token instead of reusing it."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "shared-codex",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "stale-access",
                        "refresh_token": "stale-refresh",
                    }
                ]
            },
        },
    )

    pool = CP.load_pool("openai-codex")
    stale_entry = pool.entries()[0]
    root = _read_store(root_path)
    root_entry = root["credential_pool"]["openai-codex"][0]
    root_entry["access_token"] = "winner-access"
    root_entry["refresh_token"] = "winner-refresh"
    _write_store(root_path, root)

    refresh_calls = []
    monkeypatch.setattr(A, "_codex_access_token_is_expiring", lambda token, _skew=0: False)
    monkeypatch.setattr(CP, "_codex_access_token_is_expiring", lambda token, _skew=0: False)
    monkeypatch.setattr(
        A,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: refresh_calls.append(True),
    )

    resolved = pool._refresh_entry(stale_entry, force=True)

    assert resolved is not None
    assert resolved.refresh_token == "winner-refresh"
    assert refresh_calls == []


def test_profile_singleton_seeds_profile_without_overwriting_root_pool(
    profile_and_root,
):
    """A local singleton owns a local pool and never mutates the root account."""
    profile_path, root_path = profile_and_root
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "profile-access",
                        "refresh_token": "profile-refresh",
                    }
                }
            },
            "credential_pool": {},
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "root-row",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "root-access",
                        "refresh_token": "root-refresh",
                    }
                ]
            },
        },
    )

    pool = CP.load_pool("openai-codex")

    assert pool.entries()[0].refresh_token == "profile-refresh"
    profile = _read_store(profile_path)
    assert (
        profile["credential_pool"]["openai-codex"][0]["refresh_token"]
        == "profile-refresh"
    )
    root = _read_store(root_path)
    assert (
        root["credential_pool"]["openai-codex"][0]["refresh_token"]
        == "root-refresh"
    )


def test_remove_inherited_codex_entry_updates_root(profile_and_root):
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "root-row",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "root-access",
                        "refresh_token": "root-refresh",
                    }
                ]
            },
        },
    )

    removed = CP.load_pool("openai-codex").remove_index(1)

    assert removed is not None
    assert removed.id == "root-row"
    assert not _read_store(root_path)["credential_pool"]["openai-codex"]
    assert not _read_store(profile_path)["credential_pool"].get("openai-codex")


def test_terminal_inherited_codex_refresh_quarantines_root(profile_and_root, monkeypatch):
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "dead-access",
                        "refresh_token": "dead-refresh",
                    }
                }
            },
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "root-device",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": "dead-access",
                        "refresh_token": "dead-refresh",
                    }
                ]
            },
        },
    )

    def terminal_failure(*_args, **_kwargs):
        raise A.AuthError(
            "dead",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", terminal_failure)
    pool = CP.load_pool("openai-codex")

    assert pool._refresh_entry(pool.entries()[0], force=True) is None
    root = _read_store(root_path)
    assert not root["providers"]["openai-codex"]["tokens"].get("access_token")
    assert not root["providers"]["openai-codex"]["tokens"].get("refresh_token")
    assert not root["credential_pool"]["openai-codex"]
    profile = _read_store(profile_path)
    assert not profile.get("providers", {}).get("openai-codex")
    assert not profile.get("credential_pool", {}).get("openai-codex")

