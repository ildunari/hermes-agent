import errno
import json
from pathlib import Path

import pytest

from hermes_cli.auth import AUTH_STORE_VERSION, _load_auth_store


def test_auth_store_quarantines_malformed_json(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{not-json", encoding="utf-8")

    store = _load_auth_store(auth_file)

    assert store == {"version": AUTH_STORE_VERSION, "providers": {}}
    assert auth_file.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "{not-json"


def test_auth_store_propagates_resource_exhaustion_without_quarantine(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    payload = {"version": AUTH_STORE_VERSION, "providers": {"openai-codex": {}}}
    auth_file.write_text(json.dumps(payload), encoding="utf-8")
    real_read_text = Path.read_text

    def fail_read(self, *args, **kwargs):
        if self == auth_file:
            raise OSError(errno.EMFILE, "Too many open files", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(OSError) as exc_info:
        _load_auth_store(auth_file)

    assert exc_info.value.errno == errno.EMFILE
    assert not auth_file.with_suffix(".json.corrupt").exists()