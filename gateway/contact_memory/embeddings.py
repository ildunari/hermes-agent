"""Local embedding backends.

The production EmbeddingGemma adapter deliberately runs in a subprocess whose
interpreter belongs to an isolated environment. ``mlx-embeddings`` is GPL and
must never become an import (or dependency) of the Hermes core process.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import threading
from typing import Any, Protocol, Sequence


class EmbeddingBackend(Protocol):
    model_id: str
    dimensions: int
    def encode(self, texts: Sequence[str]) -> Any: ...


EMBEDDINGGEMMA_MODEL = "mlx-community/embeddinggemma-300m-4bit"
EMBEDDINGGEMMA_VENV = Path.home() / ".cache/hermes-contact-memory/embeddinggemma-venv"


class EmbeddingWorkerError(RuntimeError):
    """The optional local embedding worker could not serve a request."""


class EmbeddingGemmaBackend:
    """Persistent JSON-lines client for isolated MLX EmbeddingGemma inference."""

    def __init__(
        self,
        model_name: str = EMBEDDINGGEMMA_MODEL,
        *,
        python_path: str | Path | None = None,
        timeout_seconds: float = 120.0,
        startup_timeout_seconds: float = 120.0,
    ) -> None:
        self.model_id = str(model_name)
        self.dimensions = 0
        self.python_path = Path(python_path or EMBEDDINGGEMMA_VENV / "bin/python").expanduser()
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.python_path.is_file():
            raise EmbeddingWorkerError(
                f"EmbeddingGemma environment not installed: {self.python_path}"
            )
        worker = Path(__file__).with_name("embeddinggemma_worker.py")
        env = os.environ.copy()
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        self._process = subprocess.Popen(
            [str(self.python_path), "-u", str(worker), "--model", self.model_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert self._process.stdout is not None
        ready, _, _ = select.select([self._process.stdout], [], [], self.startup_timeout_seconds)
        if not ready:
            self.close()
            raise EmbeddingWorkerError("EmbeddingGemma worker startup timed out")
        line = self._process.stdout.readline()
        try:
            payload = json.loads(line) if line else {}
        except json.JSONDecodeError as exc:
            self.close()
            raise EmbeddingWorkerError("EmbeddingGemma worker returned invalid readiness") from exc
        if payload.get("ready") is not True:
            detail = self._process.stderr.read().strip()[-1000:] if self._process.poll() is not None and self._process.stderr else ""
            self.close()
            raise EmbeddingWorkerError(f"EmbeddingGemma worker failed to start: {detail}".rstrip())
        return self._process

    def _request(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        clean = [str(text) for text in texts]
        if not clean:
            return []
        with self._lock:
            proc = self._start()
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps({"texts": clean, "kind": kind}) + "\n")
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], self.timeout_seconds)
                if not ready:
                    self.close()
                    raise EmbeddingWorkerError("EmbeddingGemma worker timed out")
                line = proc.stdout.readline()
                if not line:
                    detail = proc.stderr.read().strip()[-1000:] if proc.stderr else ""
                    self.close()
                    raise EmbeddingWorkerError(f"EmbeddingGemma worker exited: {detail}")
                payload = json.loads(line)
                if not payload.get("ok"):
                    raise EmbeddingWorkerError(str(payload.get("error") or "worker error"))
                vectors = payload.get("vectors")
                if not isinstance(vectors, list) or len(vectors) != len(clean):
                    raise EmbeddingWorkerError("EmbeddingGemma worker returned invalid vectors")
                result = [[float(value) for value in row] for row in vectors]
                if result:
                    self.dimensions = len(result[0])
                    if not self.dimensions or any(len(row) != self.dimensions for row in result):
                        raise EmbeddingWorkerError("EmbeddingGemma vector dimensions disagree")
                return result
            except (BrokenPipeError, json.JSONDecodeError, OSError) as exc:
                self.close()
                raise EmbeddingWorkerError(f"EmbeddingGemma worker request failed: {exc}") from exc

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self._request(texts, kind="query")

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._request(texts, kind="document")

    def close(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def __del__(self) -> None:  # pragma: no cover - best-effort process cleanup
        try:
            self.close()
        except Exception:
            pass


def backend_from_config(config: object) -> EmbeddingBackend | None:
    """Resolve an optional backend from profile ``agent.contact_memory`` config."""
    if not isinstance(config, dict):
        return None
    raw = config.get("embedding")
    if isinstance(raw, str):
        raw = {"backend": raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("backend") or "").strip().lower()
    if name in {"", "off", "none", "lexical"}:
        return None
    if name not in {"embeddinggemma", "embedding-gemma"}:
        raise ValueError(f"unknown contact-memory embedding backend: {name}")
    return EmbeddingGemmaBackend(
        str(raw.get("model") or EMBEDDINGGEMMA_MODEL),
        python_path=raw.get("python_path"),
        timeout_seconds=float(raw.get("timeout_seconds") or 120),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds") or 120),
    )


@dataclass
class HashingEmbeddingBackend:
    """Dependency-free lexical fallback; useful for tests, not model selection."""
    dimensions: int = 256
    model_id: str = "hermes-hashing-v1"

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np  # type: ignore[import-not-found]
        matrix = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = re.findall(r"[\w']+", str(text).lower())
            for token in tokens:
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                matrix[row, index] += -1.0 if digest[4] & 1 else 1.0
            norm = np.linalg.norm(matrix[row])
            if norm:
                matrix[row] /= norm
        return matrix


class Model2VecBackend:
    """Optional adapter loaded only from an isolated local environment."""
    def __init__(self, model_name: str = "minishlab/potion-retrieval-32M"):
        import numpy as np  # type: ignore[import-not-found]
        from model2vec import StaticModel  # type: ignore[import-not-found]
        self._model = StaticModel.from_pretrained(model_name)
        self.model_id = model_name
        probe = np.asarray(self._model.encode(["probe"]), dtype=np.float32)
        self.dimensions = int(probe.shape[1])

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np  # type: ignore[import-not-found]
        matrix = np.asarray(self._model.encode(list(texts)), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return np.ascontiguousarray(matrix / np.maximum(norms, 1e-12), dtype=np.float32)
