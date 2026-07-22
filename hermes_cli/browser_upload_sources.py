"""Memory-only, descriptor-owned Studio sources for in-app browser uploads.

Paths never leave this authority. Candidate discovery returns opaque ids and safe
metadata; selecting one opens the recorded file beneath its server-authoritative
workspace root and keeps that exact descriptor through hashing and one raw stream.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable, Iterator


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_VALUE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATES = 256
_MAX_GRANTS = 128
_MAX_CHUNK_BYTES = 1024 * 1024
_DEFAULT_TTL_SECONDS = 120
_MAX_TTL_SECONDS = 120
_MAX_TRANSFER_SECONDS = 5 * 60


class BrowserUploadSourceError(Exception):
    """A typed failure whose code is safe to disclose without path details."""

    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class BrowserUploadSourceScope:
    recipient: str
    profile: str
    connection_id: str
    transport_id: str
    browser_sid: str
    capability_generation: str
    task_id: str
    task_generation: str
    tab_id: str
    tab_incarnation: str
    binding_generation: str
    document_generation: str
    frame_id: str
    origin: str
    chooser_id: str
    backend_node_id: str
    form_fingerprint: str
    chooser_mode: str
    source_session_id: str

    def validated(self) -> "BrowserUploadSourceScope":
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if not all(isinstance(value, str) and _VALUE_RE.fullmatch(value) for value in values):
            raise BrowserUploadSourceError("invalid_scope")
        if self.chooser_mode not in {"selectSingle", "selectMultiple"}:
            raise BrowserUploadSourceError("invalid_scope")
        return self


@dataclass(frozen=True)
class BrowserUploadCandidate:
    candidate_id: str
    source_record_revision: str
    scope: BrowserUploadSourceScope
    root: Path
    relative_path: Path
    display_name: str
    mime_type: str
    size: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    expires_at: float


@dataclass
class _HeldSource:
    ref: str
    credential: str
    source_record_revision: str
    scope: BrowserUploadSourceScope
    fd: int
    display_name: str
    mime_type: str
    size: int
    sha256: str
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    expires_at: float
    claimed: bool = False
    closed: bool = False
    transfer_deadline: float | None = None


@dataclass(frozen=True)
class BrowserUploadSourceTicket:
    ref: str
    credential: str
    source_record_revision: str
    recipient: str
    display_name: str
    mime_type: str
    size: int
    sha256: str
    expires_at: float


class BrowserUploadSourceRead:
    """One claimed descriptor. Closing always releases concurrency and the fd."""

    def __init__(self, authority: "BrowserUploadSourceAuthority", source: _HeldSource):
        self._authority = authority
        self._source = source
        self._closed = False

    @property
    def display_name(self) -> str:
        return self._source.display_name

    @property
    def mime_type(self) -> str:
        return self._source.mime_type

    @property
    def size(self) -> int:
        return self._source.size

    @property
    def sha256(self) -> str:
        return self._source.sha256

    def chunks(self, chunk_bytes: int = _MAX_CHUNK_BYTES) -> Iterator[bytes]:
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or not 1 <= chunk_bytes <= _MAX_CHUNK_BYTES:
            raise BrowserUploadSourceError("UPLOAD_TRANSFER_FAILED")
        remaining = self._source.size
        try:
            self._authority._start_transfer(self._source)
            while remaining:
                chunk = self._authority._read_transfer_chunk(
                    self._source, min(chunk_bytes, remaining)
                )
                if not chunk:
                    raise BrowserUploadSourceError("UPLOAD_SOURCE_MUTATED", 409)
                remaining -= len(chunk)
                yield chunk
            self._authority._finish_transfer(self._source)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._authority._release_claim(self._source)


class BrowserUploadSourceAuthority:
    """Thread-safe authority for candidate ids and one-use raw source streams."""

    def __init__(
        self,
        *,
        workspace_root: Callable[[str, str], Path | None],
        clock: Callable[[], float] = time.monotonic,
        token: Callable[[int], str] = secrets.token_urlsafe,
        transfer_seconds: float = _MAX_TRANSFER_SECONDS,
    ) -> None:
        if (
            isinstance(transfer_seconds, bool)
            or not isinstance(transfer_seconds, (int, float))
            or not 0 < transfer_seconds <= _MAX_TRANSFER_SECONDS
        ):
            raise ValueError("transfer_seconds must be positive and at most five minutes")
        self._workspace_root = workspace_root
        self._clock = clock
        self._token = token
        self._transfer_seconds = float(transfer_seconds)
        self._lock = threading.RLock()
        self._candidates: dict[str, BrowserUploadCandidate] = {}
        self._grants: dict[str, _HeldSource] = {}
        self._active: dict[str, _HeldSource] = {}
        self._active_connections: dict[str, int] = {}
        self._active_choosers: set[tuple[str, str]] = set()
        self._deadline_timer: threading.Timer | None = None
        self._deadline_generation = 0

    def _secret(self) -> str:
        value = self._token(24)
        if not _ID_RE.fullmatch(value):
            raise BrowserUploadSourceError("entropy_unavailable", 503)
        return value

    def _expiry(self, ttl_seconds: int) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise BrowserUploadSourceError("invalid_expiry")
        return self._clock() + ttl_seconds

    def _purge_locked(self) -> None:
        now = self._clock()
        for candidate_id, candidate in list(self._candidates.items()):
            if candidate.expires_at <= now:
                del self._candidates[candidate_id]
        for ref, source in list(self._grants.items()):
            if source.expires_at <= now and not source.claimed:
                del self._grants[ref]
                self._close_source_locked(source)

    def list_candidates(
        self, scope: BrowserUploadSourceScope, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS
    ) -> list[BrowserUploadCandidate]:
        scope = scope.validated()
        root = self._workspace_root(scope.profile, scope.source_session_id)
        if root is None:
            raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 404)
        try:
            root = Path(root).expanduser().resolve(strict=True)
            if not root.is_dir() or root.is_symlink():
                raise OSError("unsupported root")
        except (OSError, RuntimeError):
            raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403) from None

        discovered: list[BrowserUploadCandidate] = []
        expiry = self._expiry(ttl_seconds)
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_names[:] = sorted(
                name for name in directory_names if not (directory_path / name).is_symlink()
            )
            for name in sorted(file_names):
                if len(discovered) >= _MAX_CANDIDATES:
                    break
                path = directory_path / name
                try:
                    info = path.lstat()
                    relative = path.relative_to(root)
                    _validate_file_metadata(path, info)
                except (OSError, ValueError, BrowserUploadSourceError):
                    continue
                candidate = BrowserUploadCandidate(
                    candidate_id=self._secret(),
                    source_record_revision=self._secret(),
                    scope=scope,
                    root=root,
                    relative_path=relative,
                    display_name=_safe_display_name(name),
                    mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    size=info.st_size,
                    device=info.st_dev,
                    inode=info.st_ino,
                    modified_ns=info.st_mtime_ns,
                    changed_ns=info.st_ctime_ns,
                    expires_at=expiry,
                )
                discovered.append(candidate)
            if len(discovered) >= _MAX_CANDIDATES:
                break
        with self._lock:
            self._purge_locked()
            for candidate in discovered:
                self._candidates[candidate.candidate_id] = candidate
        return discovered

    def mint(
        self,
        scope: BrowserUploadSourceScope,
        candidate_id: str,
        source_record_revision: str | None = None,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> BrowserUploadSourceTicket:
        scope = scope.validated()
        if not _ID_RE.fullmatch(candidate_id or ""):
            raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 404)
        with self._lock:
            self._purge_locked()
            candidate = self._candidates.pop(candidate_id, None)
        if (
            candidate is None
            or candidate.scope != scope
            or (source_record_revision is not None and not hmac.compare_digest(
                candidate.source_record_revision, source_record_revision
            ))
        ):
            raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 404)

        fd = -1
        try:
            fd = _open_recorded_candidate(candidate)
            info = os.fstat(fd)
            _verify_record(candidate, info)
            digest = hashlib.sha256()
            count = 0
            while count < info.st_size:
                chunk = os.read(fd, min(_MAX_CHUNK_BYTES, info.st_size - count))
                if not chunk:
                    raise BrowserUploadSourceError("UPLOAD_SOURCE_MUTATED", 409)
                digest.update(chunk)
                count += len(chunk)
            after_hash = os.fstat(fd)
            _verify_record(candidate, after_hash)
            if count != info.st_size:
                raise BrowserUploadSourceError("UPLOAD_SOURCE_MUTATED", 409)
            os.lseek(fd, 0, os.SEEK_SET)
            source = _HeldSource(
                ref=self._secret(),
                credential=self._secret(),
                source_record_revision=candidate.source_record_revision,
                scope=scope,
                fd=fd,
                display_name=candidate.display_name,
                mime_type=candidate.mime_type,
                size=info.st_size,
                sha256=digest.hexdigest(),
                device=info.st_dev,
                inode=info.st_ino,
                modified_ns=info.st_mtime_ns,
                changed_ns=info.st_ctime_ns,
                expires_at=self._expiry(ttl_seconds),
            )
            with self._lock:
                self._purge_locked()
                if len(self._grants) >= _MAX_GRANTS:
                    raise BrowserUploadSourceError("grant_capacity", 429)
                self._grants[source.ref] = source
            fd = -1
            return BrowserUploadSourceTicket(
                ref=source.ref,
                credential=source.credential,
                source_record_revision=source.source_record_revision,
                recipient=scope.recipient,
                display_name=source.display_name,
                mime_type=source.mime_type,
                size=source.size,
                sha256=source.sha256,
                expires_at=source.expires_at,
            )
        finally:
            if fd >= 0:
                os.close(fd)

    def claim(
        self,
        ref: str,
        credential: str,
        scope: BrowserUploadSourceScope,
        source_record_revision: str | None = None,
    ) -> BrowserUploadSourceRead:
        scope = scope.validated()
        if not _ID_RE.fullmatch(ref or "") or not _ID_RE.fullmatch(credential or ""):
            raise BrowserUploadSourceError("grant_invalid", 401)
        with self._lock:
            self._purge_locked()
            source = self._grants.get(ref)
            if source is None:
                raise BrowserUploadSourceError("grant_unavailable", 404)
            if not hmac.compare_digest(source.credential, credential):
                raise BrowserUploadSourceError("grant_invalid", 401)
            if not _ID_RE.fullmatch(source_record_revision or "") or not hmac.compare_digest(
                source.source_record_revision, source_record_revision or ""
            ):
                del self._grants[ref]
                self._close_source_locked(source)
                raise BrowserUploadSourceError("grant_scope_mismatch", 403)
            if source.scope != scope:
                del self._grants[ref]
                self._close_source_locked(source)
                raise BrowserUploadSourceError("grant_scope_mismatch", 403)
            connection_count = self._active_connections.get(scope.connection_id, 0)
            chooser_key = (scope.connection_id, scope.chooser_id)
            if connection_count >= 2 or chooser_key in self._active_choosers:
                raise BrowserUploadSourceError("grant_capacity", 429)
            try:
                self._verify_unchanged(source)
            except Exception:
                del self._grants[ref]
                self._close_source_locked(source)
                raise
            source.claimed = True
            source.transfer_deadline = self._clock() + self._transfer_seconds
            del self._grants[ref]
            self._active[ref] = source
            self._active_connections[scope.connection_id] = connection_count + 1
            self._active_choosers.add(chooser_key)
            self._schedule_deadline_locked()
        return BrowserUploadSourceRead(self, source)

    def _require_active_locked(self, source: _HeldSource) -> None:
        if source.closed or self._active.get(source.ref) is not source:
            raise BrowserUploadSourceError("UPLOAD_EXPIRED", 410)
        self._verify_transfer_deadline(source)

    def _start_transfer(self, source: _HeldSource) -> None:
        # Descriptor validity, positioning, reads, and closes share this lock.
        # A lifecycle close can therefore never turn a cached descriptor number
        # into authority over a subsequently opened, unrelated file.
        with self._lock:
            self._require_active_locked(source)
            try:
                os.lseek(source.fd, 0, os.SEEK_SET)
            except OSError:
                raise BrowserUploadSourceError("UPLOAD_TRANSFER_FAILED", 409) from None

    def _read_transfer_chunk(self, source: _HeldSource, chunk_bytes: int) -> bytes:
        with self._lock:
            self._require_active_locked(source)
            try:
                chunk = os.read(source.fd, chunk_bytes)
            except OSError:
                raise BrowserUploadSourceError("UPLOAD_TRANSFER_FAILED", 409) from None
            self._require_active_locked(source)
            return chunk

    def _finish_transfer(self, source: _HeldSource) -> None:
        with self._lock:
            self._require_active_locked(source)
            self._verify_unchanged(source)

    def _schedule_deadline_locked(self) -> None:
        self._deadline_generation += 1
        generation = self._deadline_generation
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None
        deadlines = [source.transfer_deadline for source in self._active.values()]
        deadlines = [deadline for deadline in deadlines if deadline is not None]
        if not deadlines:
            return
        delay = max(0.001, min(deadlines) - self._clock())
        timer = threading.Timer(delay, self._expire_claims, args=(generation,))
        timer.daemon = True
        self._deadline_timer = timer
        timer.start()

    def _expire_claims(self, generation: int) -> None:
        with self._lock:
            if generation != self._deadline_generation:
                return
            self._deadline_timer = None
            now = self._clock()
            expired = [
                source
                for source in self._active.values()
                if source.transfer_deadline is None or now >= source.transfer_deadline
            ]
            for source in expired:
                self._release_claim_locked(source)
            self._schedule_deadline_locked()

    def _verify_unchanged(self, source: _HeldSource) -> None:
        try:
            info = os.fstat(source.fd)
        except OSError:
            raise BrowserUploadSourceError("UPLOAD_TRANSFER_FAILED", 409) from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != source.size
            or info.st_dev != source.device
            or info.st_ino != source.inode
            or info.st_mtime_ns != source.modified_ns
            or info.st_ctime_ns != source.changed_ns
        ):
            raise BrowserUploadSourceError("UPLOAD_SOURCE_MUTATED", 409)

    def _verify_transfer_deadline(self, source: _HeldSource) -> None:
        if source.transfer_deadline is None or self._clock() >= source.transfer_deadline:
            raise BrowserUploadSourceError("UPLOAD_EXPIRED", 410)

    def _release_claim(self, source: _HeldSource) -> None:
        with self._lock:
            self._release_claim_locked(source)

    def _release_claim_locked(self, source: _HeldSource) -> None:
        if self._active.get(source.ref) is source:
            del self._active[source.ref]
            connection = source.scope.connection_id
            remaining = self._active_connections.get(connection, 1) - 1
            if remaining > 0:
                self._active_connections[connection] = remaining
            else:
                self._active_connections.pop(connection, None)
            self._active_choosers.discard((connection, source.scope.chooser_id))
        self._close_source_locked(source)

    @staticmethod
    def _close_source_locked(source: _HeldSource) -> None:
        if source.closed:
            return
        source.closed = True
        os.close(source.fd)

    def revoke_where(self, **parts: str) -> int:
        allowed = set(BrowserUploadSourceScope.__dataclass_fields__)
        if not parts or not set(parts).issubset(allowed):
            raise BrowserUploadSourceError("invalid_scope")
        with self._lock:
            self._purge_locked()
            candidates = [key for key, value in self._candidates.items() if all(getattr(value.scope, k) == v for k, v in parts.items())]
            grants = [key for key, value in self._grants.items() if all(getattr(value.scope, k) == v for k, v in parts.items())]
            active = [key for key, value in self._active.items() if all(getattr(value.scope, k) == v for k, v in parts.items())]
            for key in candidates:
                del self._candidates[key]
            for key in grants:
                source = self._grants.pop(key)
                self._close_source_locked(source)
            for key in active:
                self._release_claim_locked(self._active[key])
            return len(candidates) + len(grants) + len(active)

    def revoke_grants(self, scope: BrowserUploadSourceScope, refs: list[str]) -> int:
        """Revoke only unclaimed grants matching one complete authenticated tuple."""
        scope = scope.validated()
        if not refs or len(refs) > 20 or any(not _ID_RE.fullmatch(ref or "") for ref in refs):
            raise BrowserUploadSourceError("invalid_scope")
        with self._lock:
            self._purge_locked()
            revoked = 0
            for ref in set(refs):
                source = self._grants.get(ref)
                if source is None or source.scope != scope:
                    continue
                del self._grants[ref]
                self._close_source_locked(source)
                revoked += 1
            return revoked

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._candidates) + len(self._grants) + len(self._active)
            self._candidates.clear()
            grants = [*self._grants.values(), *self._active.values()]
            self._grants.clear()
            self._active.clear()
            self._active_connections.clear()
            self._active_choosers.clear()
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
                self._deadline_timer = None
            self._deadline_generation += 1
            for source in grants:
                self._close_source_locked(source)
            return count


def _safe_display_name(name: str) -> str:
    if not name or name in {".", ".."} or len(name.encode("utf-8")) > 255:
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)
    return name


def _validate_file_metadata(path: Path, info: os.stat_result, fd: int | None = None) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)
    if info.st_size > _MAX_FILE_BYTES:
        raise BrowserUploadSourceError("UPLOAD_TOO_LARGE", 413)
    owned_fd = -1
    try:
        if fd is None:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            owned_fd = os.open(path, flags)
            fd = owned_fd
        if _descriptor_is_sparse(fd, info):
            raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)
    except OSError:
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403) from None
    finally:
        if owned_fd >= 0:
            os.close(owned_fd)
    _safe_display_name(path.name)
    from agent.file_safety import get_read_block_error

    if get_read_block_error(str(path)) is not None:
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)


def _descriptor_is_sparse(fd: int, info: os.stat_result) -> bool:
    if info.st_size == 0:
        return False
    # SEEK_DATA/SEEK_HOLE catches APFS sparse gaps even when allocation rounding
    # makes st_blocks*512 exceed logical size. Fall back conservatively where the
    # filesystem does not implement those seeks.
    seek_data = getattr(os, "SEEK_DATA", None)
    seek_hole = getattr(os, "SEEK_HOLE", None)
    if seek_data is not None and seek_hole is not None:
        try:
            position = 0
            while position < info.st_size:
                data = os.lseek(fd, position, seek_data)
                if data > position:
                    return True
                hole = os.lseek(fd, data, seek_hole)
                if hole < info.st_size:
                    next_data = os.lseek(fd, hole, seek_data)
                    if next_data > hole:
                        return True
                position = max(hole, data + 1)
            os.lseek(fd, 0, os.SEEK_SET)
            return False
        except OSError:
            os.lseek(fd, 0, os.SEEK_SET)
    allocated = getattr(info, "st_blocks", None)
    return isinstance(allocated, int) and allocated * 512 < info.st_size


def _open_recorded_candidate(candidate: BrowserUploadCandidate) -> int:
    # Candidate ids, not caller paths, select this recorded relative path. Walk
    # from an opened root with no-follow openat calls so a directory-component
    # swap cannot redirect the final open outside the authorized tree.
    parts = candidate.relative_path.parts
    if not parts or ".." in parts:
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    leaf_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fds: list[int] = []
    fd = -1
    try:
        current_fd = os.open(candidate.root, directory_flags)
        directory_fds.append(current_fd)
        for component in parts[:-1]:
            current_fd = os.open(component, directory_flags, dir_fd=current_fd)
            directory_fds.append(current_fd)
        fd = os.open(parts[-1], leaf_flags, dir_fd=current_fd)
        _validate_file_metadata(candidate.root / candidate.relative_path, os.fstat(fd), fd)
        return fd
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise BrowserUploadSourceError("UPLOAD_SOURCE_BLOCKED", 403) from None
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _verify_record(candidate: BrowserUploadCandidate, info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size != candidate.size
        or info.st_dev != candidate.device
        or info.st_ino != candidate.inode
        or info.st_mtime_ns != candidate.modified_ns
        or info.st_ctime_ns != candidate.changed_ns
    ):
        raise BrowserUploadSourceError("UPLOAD_SOURCE_MUTATED", 409)
