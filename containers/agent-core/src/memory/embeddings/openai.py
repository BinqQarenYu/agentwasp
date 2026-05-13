"""OpenAI embedding provider — calls /v1/embeddings via httpx (no openai SDK needed)."""
from __future__ import annotations

import math

import httpx
import structlog

from .base import EmbeddingProvider

logger = structlog.get_logger()

_EMBED_DIM = 384
_OPENAI_URL = "https://api.openai.com/v1/embeddings"


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Generates embeddings via the OpenAI /v1/embeddings API.

    Uses httpx directly — no openai SDK dependency required.
    Model defaults to text-embedding-3-small (1536 dims, truncated to _EMBED_DIM).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return f"openai:{self._model}"

    async def embed(self, text: str) -> list[float] | None:
        """Call OpenAI /v1/embeddings and return a normalised vector.

        Returns None on any API error, allowing the caller to activate fallback.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _OPENAI_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self._model, "input": text[:8000]},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vec = data["data"][0]["embedding"]
                    if vec:
                        trunc = vec[:_EMBED_DIM] if len(vec) > _EMBED_DIM else vec
                        return _normalise(trunc)
                else:
                    logger.warning(
                        "openai_provider.api_error",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
        except Exception as exc:
            logger.debug("openai_provider.embed_failed", error=str(exc)[:120])
        return None
