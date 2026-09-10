"""Native catalog credentials and background writes belong to the requesting profile."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
import hermes_cli.models as models


@contextmanager
def profile_scope(home, key):
    home.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(home)
    secret = set_secret_scope({"OPENAI_API_KEY": key, "OPENAI_BASE_URL": "https://catalog.invalid/v1"})
    try:
        yield
    finally:
        reset_secret_scope(secret)
        reset_hermes_home_override(token)


def test_native_openai_catalog_and_fingerprint_use_concurrent_profile_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "root-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://root.invalid/v1")
    barrier = threading.Barrier(2)
    seen = []
    def fetch(key, base, **kwargs):
        barrier.wait(timeout=5)
        seen.append((key, base))
        return [key + "-model"]
    monkeypatch.setattr(models, "fetch_api_models", fetch)
    home = tmp_path / "shared-home"
    def run(key):
        with profile_scope(home, key):
            return models._openai_catalog("openai", False), models._credential_fingerprint("openai")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["profile-a", "profile-b"]))
    assert sorted(seen) == [(key, "https://catalog.invalid/v1") for key in ["profile-a", "profile-b"]]
    assert [r[0] for r in results] == [["profile-a-model"], ["profile-b-model"]]
    assert results[0][1] != results[1][1]
    from hermes_cli.models_local import _api_key_from_provider_config
    monkeypatch.setenv("LOCAL_CATALOG_KEY", "root-key")
    token = set_secret_scope({"LOCAL_CATALOG_KEY": "profile-local-key"})
    try:
        assert _api_key_from_provider_config({"key_env": "LOCAL_CATALOG_KEY"}, "key_env") == "profile-local-key"
    finally:
        reset_secret_scope(token)


@pytest.mark.parametrize("custom", [False, True])
def test_swr_keeps_profile_credentials_cache_path_and_independent_inflight_slots(monkeypatch, tmp_path, custom):
    from agent.secret_scope import get_secret
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("OPENAI_API_KEY", "root-key")
    started = threading.Barrier(3)
    release = threading.Event()
    done = {key: threading.Event() for key in ["profile-a", "profile-b"]}
    real_store = models._store_cache_entry
    def store(key, entry, *args):
        real_store(key, entry, *args)
        done[entry["models"][0]].set()
    monkeypatch.setattr(models, "_store_cache_entry", store)
    def fetch(*args, **kwargs):
        started.wait(timeout=5)
        release.wait(timeout=5)
        return [get_secret("OPENAI_API_KEY")]
    monkeypatch.setattr(models, "provider_model_ids", fetch)
    cache_key = "custom:https://catalog.invalid/v1#same-key" if custom else "openai"
    def custom_refresh():
        return models._cache_entry("fp", fetch())
    try:
        for key in done:
            with profile_scope(tmp_path / key, key):
                models._spawn_swr_refresh(cache_key, custom_refresh if custom else None)
        started.wait(timeout=5)
    finally:
        release.set()
    for key, event in done.items():
        assert event.wait(timeout=5)
        data = json.loads((tmp_path / key / "provider_models_cache.json").read_text())
        assert data[cache_key]["models"] == [key]
    assert not (tmp_path / "root" / "provider_models_cache.json").exists()
