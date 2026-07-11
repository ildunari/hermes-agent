"""Local embedding backends. Core has no Model2Vec or NumPy dependency."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Protocol, Sequence


class EmbeddingBackend(Protocol):
    model_id: str
    dimensions: int
    def encode(self, texts: Sequence[str]) -> Any: ...


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
