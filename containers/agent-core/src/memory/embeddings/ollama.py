"""Ollama embedding provider — calls /api/embeddings on a local Ollama instance."""
from __future__ import annotations

import math
import struct

import httpx
import structlog

from .base import EmbeddingProvider

logger = structlog.get_logger()

_EMBED_DIM = 384  # Normalised output dimensionality


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Generates embeddings via an Ollama /api/embeddings endpoint."""

    def __init__(
        self,
        ollama_url: str = "http://agent-ollama:11434",
        model: str = "nomic-embed-text",
        timeout: float = 10.0,
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, text: str) -> list[float] | None:
        """POST to Ollama /api/embeddings and return a normalised vector.

        Returns None when Ollama is offline or the model is not available,
        allowing the caller to activate hash-fallback.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/embeddings",
                    json={"model": self._model, "prompt": text[:4000]},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vec = data.get("embedding", [])
                    if vec:
                        trunc = vec[:_EMBED_DIM] if len(vec) > _EMBED_DIM else vec
                        return _normalise(trunc)
        except Exception as exc:
            logger.debug("ollama_provider.embed_failed", error=str(exc)[:120])
        return None
