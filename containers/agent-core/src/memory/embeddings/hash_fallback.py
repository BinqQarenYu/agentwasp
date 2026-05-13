"""Hash-based fallback embedding provider — always available, non-semantic.

Used when no real embedding provider is configured or available.
Produces deterministic, stable vectors from SHA-512 text hashes.
Vectors are NOT semantically meaningful — cosine similarity will be random.
"""
from __future__ import annotations

import hashlib
import math
import struct

import structlog

from .base import EmbeddingProvider

logger = structlog.get_logger()

_EMBED_DIM = 384


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class HashFallbackEmbeddingProvider(EmbeddingProvider):
    """Deterministic pseudo-embedding from SHA-512 text hash.

    Always returns a vector (never None). is_semantic = False signals to callers
    that retrieval quality is degraded and a warning should be logged.
    """

    def __init__(self, dims: int = _EMBED_DIM) -> None:
        self._dims = dims

    @property
    def model_name(self) -> str:
        return "hash-fallback"

    @property
    def is_semantic(self) -> bool:
        return False

    async def embed(self, text: str) -> list[float] | None:
        raw = hashlib.sha512(text.encode()).digest()
        needed = self._dims * 4
        repeated = (raw * (needed // len(raw) + 1))[:needed]
        floats = [struct.unpack_from("f", repeated, i * 4)[0] for i in range(self._dims)]
        floats = [0.0 if not math.isfinite(f) else f for f in floats]
        return _normalise(floats)
