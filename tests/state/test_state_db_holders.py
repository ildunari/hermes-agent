"""Behavioral tests for the state-holder and repair-admission authority."""

import os

import pytest

import hermes_state_holders


@pytest.mark.linux_only
def test_foreign_holder_accepts_same_inode_reached_through_an_alias(
    tmp_path, monkeypatch
):
    """Descriptor identity is authoritative even when /proc spells another path."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    alias_path = tmp_path / "namespace-alias" / "state.db"

    proc_root = tmp_path / "proc"
    for pid in (111, 222):
        (proc_root / str(pid) / "fd").mkdir(parents=True)
    os.symlink(db_path, proc_root / "222" / "fd" / "3")

    monkeypatch.setattr(hermes_state_holders.os, "getpid", lambda: 111)
    real_listdir = os.listdir

    def _listdir(path):
        if isinstance(path, str):
            path = path.replace("/proc", str(proc_root))
        return real_listdir(path)

    monkeypatch.setattr(hermes_state_holders.os, "listdir", _listdir)

    def _readlink(path):
        if path == "/proc/222/fd/3":
            return str(alias_path)
        return os.readlink(path.replace("/proc", str(proc_root)))

    monkeypatch.setattr(hermes_state_holders.os, "readlink", _readlink)
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        path = str(path).replace("/proc", str(proc_root))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(hermes_state_holders.os, "stat", _stat)

    assert hermes_state_holders.foreign_state_db_holders(db_path) == [
        (222, str(alias_path))
    ]


@pytest.mark.parametrize('result,error,empty', [(0, 0, True), (0, 1, False), (-1, 0, False), (8, 0, False)])
def test_darwin_empty_fd_proof_requires_success(monkeypatch, result, error, empty):
    import ctypes
    from types import SimpleNamespace
    monkeypatch.setattr(hermes_state_holders.sys, 'platform', 'darwin')
    def query(pid, flavor, arg, buffer, size):
        assert (pid, flavor, size) == (123, 1, 8)
        assert ctypes.get_errno() == 0
        ctypes.set_errno(error)
        return result
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **k: SimpleNamespace(proc_pidinfo=query))
    assert hermes_state_holders._darwin_proves_empty_fd_table(123) is empty


@pytest.mark.macos_only
@pytest.mark.parametrize('proven_empty', [False, True])
def test_darwin_psutil_zero_fd_failure_keeps_other_holders(tmp_path, monkeypatch, proven_empty):
    from types import SimpleNamespace
    db_path = tmp_path / 'state.db'
    db_path.touch()
    def fail():
        raise RuntimeError('proc_pidinfo(PROC_PIDLISTFDS) 2/2 syscall failed')
    processes = [SimpleNamespace(info={'pid': 123}, open_files=fail),
                 SimpleNamespace(info={'pid': 124}, open_files=lambda: [SimpleNamespace(path=str(db_path))])]
    monkeypatch.setattr(hermes_state_holders.psutil, 'process_iter', lambda attrs: iter(processes))
    monkeypatch.setattr(hermes_state_holders, '_darwin_proves_empty_fd_table', lambda pid: proven_empty)
    holders = hermes_state_holders.foreign_state_db_holders(db_path)
    if proven_empty:
        assert holders == [(124, str(db_path))]
    else:
        assert holders[0][0] == -1
        assert 'open-file scan failed' in holders[0][1]


@pytest.mark.macos_only
def test_darwin_real_process_with_open_fd_is_not_empty(tmp_path):
    with (tmp_path / 'held').open('w'):
        assert not hermes_state_holders._darwin_proves_empty_fd_table(os.getpid())
