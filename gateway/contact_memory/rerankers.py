"""Optional local second-stage rerankers for contact-aware retrieval."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import threading
from typing import Protocol, Sequence

from .embeddings import EMBEDDINGGEMMA_VENV

QWEN3_RERANKER_MODEL = "mlx-community/Qwen3-Reranker-0.6B-4bit"


class Reranker(Protocol):
    model_id: str

    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


class RerankerWorkerError(RuntimeError):
    """The optional reranker worker could not serve a request."""


class Qwen3RerankerBackend:
    """Persistent isolated MLX Qwen3 yes/no-logit reranker client."""

    def __init__(
        self,
        model_name: str = QWEN3_RERANKER_MODEL,
        *,
        python_path: str | Path | None = None,
        timeout_seconds: float = 0.35,
        startup_timeout_seconds: float = 120.0,
        max_length: int = 2048,
        instruction: str | None = None,
    ) -> None:
        self.model_id = str(model_name)
        self.python_path = Path(
            python_path or EMBEDDINGGEMMA_VENV / "bin/python"
        ).expanduser()
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.max_length = max(128, int(max_length))
        self.instruction = instruction
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _readline(proc: subprocess.Popen[str], timeout: float) -> str:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError
        return proc.stdout.readline()

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.python_path.is_file():
            raise RerankerWorkerError(f"reranker environment not installed: {self.python_path}")
        worker = Path(__file__).with_name("qwen3_reranker_worker.py")
        env = os.environ.copy()
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        self._process = subprocess.Popen(
            [
                str(self.python_path), "-u", str(worker), "--model", self.model_id,
                "--max-length", str(self.max_length),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        try:
            line = self._readline(self._process, self.startup_timeout_seconds)
            payload = json.loads(line) if line else {}
            if payload.get("ready") is not True:
                raise ValueError("invalid readiness response")
            # Compile both the shared-prefix prefill and top-10 scoring graphs
            # before declaring startup complete. Otherwise the first live query
            # would consume the strict inference budget and repeatedly fail.
            assert self._process.stdin is not None
            warmups = (
                "Which current contact fact answers this question?",
                "Is this contact fact an explicit correction?",
            )
            for warm_query in warmups:
                warmup: dict[str, object] = {
                    "query": warm_query,
                    "documents": [f"Synthetic contact fact number {index}." for index in range(10)],
                }
                if self.instruction:
                    warmup["instruction"] = self.instruction
                self._process.stdin.write(json.dumps(warmup, separators=(",", ":")) + "\n")
                self._process.stdin.flush()
                warm_line = self._readline(self._process, self.startup_timeout_seconds)
                warm_payload = json.loads(warm_line) if warm_line else {}
                if warm_payload.get("ok") is not True:
                    raise ValueError("reranker warmup failed")
        except (TimeoutError, json.JSONDecodeError, ValueError) as exc:
            detail = ""
            if self._process.poll() is not None and self._process.stderr:
                detail = self._process.stderr.read().strip()[-1000:]
            self.close()
            message = "reranker worker startup timed out" if isinstance(exc, TimeoutError) else "reranker worker failed to start"
            raise RerankerWorkerError(f"{message}: {detail}".rstrip()) from exc
        return self._process

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        clean = [str(document) for document in documents]
        if not clean:
            return []
        with self._lock:
            proc = self._start()
            assert proc.stdin is not None
            request: dict[str, object] = {"query": str(query), "documents": clean}
            if self.instruction:
                request["instruction"] = self.instruction
            try:
                proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                proc.stdin.flush()
                line = self._readline(proc, self.timeout_seconds)
                if not line:
                    raise OSError("worker exited")
                payload = json.loads(line)
                scores = payload.get("scores")
                if not payload.get("ok") or not isinstance(scores, list) or len(scores) != len(clean):
                    raise RerankerWorkerError(str(payload.get("error") or "invalid reranker response"))
                return [float(score) for score in scores]
            except TimeoutError as exc:
                self.close()
                raise RerankerWorkerError("reranker worker timed out") from exc
            except (BrokenPipeError, json.JSONDecodeError, OSError, ValueError) as exc:
                self.close()
                raise RerankerWorkerError(f"reranker worker request failed: {exc}") from exc

    def close(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


def reranker_from_config(config: object) -> Reranker | None:
    """Resolve an optional reranker from profile ``agent.contact_memory`` config."""
    if not isinstance(config, dict):
        return None
    raw = config.get("reranker")
    if isinstance(raw, str):
        raw = {"backend": raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("backend") or "").strip().lower()
    if name in {"", "off", "none"}:
        return None
    if name not in {"qwen3", "qwen3-mlx", "qwen3-reranker"}:
        raise ValueError(f"unknown contact-memory reranker backend: {name}")
    return Qwen3RerankerBackend(
        str(raw.get("model") or QWEN3_RERANKER_MODEL),
        python_path=raw.get("python_path"),
        timeout_seconds=float(raw.get("timeout_seconds") or 0.35),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds") or 120),
        max_length=int(raw.get("max_length") or 2048),
        instruction=str(raw["instruction"]) if raw.get("instruction") else None,
    )
