import sys

import pytest


class _FakeOptimizeDB:
    def __init__(self, db_path, *, result=None, error=None):
        self.db_path = db_path
        self.result = result
        self.error = error
        self.closed = False

    def fts_optimize_available(self):
        return True

    def optimize_fts_storage(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [
        (None, RuntimeError("boom"), "optimization failed: boom"),
        ({"ok": False, "reason": "fts5_unavailable"}, None, "fts5_unavailable"),
    ],
)
def test_optimize_storage_errors_exit_nonzero(
    monkeypatch, tmp_path, capsys, result, error, expected
):
    import hermes_cli.main as main_mod
    import hermes_state

    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"small")
    fake = _FakeOptimizeDB(db_path, result=result, error=error)
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: fake)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "sessions", "optimize-storage", "--yes", "--no-vacuum"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    assert expected in capsys.readouterr().out
    assert fake.closed is True
