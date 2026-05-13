"""Abstract base class for embedding providers."""
from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Contract that every embedding backend must satisfy.

    embed() returns a normalised float vector on success, or None on failure.
    Callers must always handle None and apply a fallback.
    """

    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        """Generate a normalised embedding vector for *text*.

        Returns:
            list[float] — normalised unit vector of self.dims dimensions.
            None        — provider unavailable or model not found.
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Human-readable model identifier stored in embed_model column."""

    @property
    def is_semantic(self) -> bool:
        """True when the provider produces semantically meaningful vectors.

        HashFallbackEmbeddingProvider returns False here, allowing callers to
        emit a [DEGRADED] warning when semantic retrieval quality is reduced.
        """
        return True
