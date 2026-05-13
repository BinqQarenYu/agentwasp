from abc import ABC, abstractmethod

from .types import ModelRequest, ModelResponse


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...

    @abstractmethod
    def available_models(self) -> list[str]:
        ...

    @abstractmethod
    def provider_name(self) -> str:
        ...

    def supports_vision(self, model: str = "") -> bool:
        """Return True if the given model (or active model) supports image input."""
        return False

    def supports_audio(self, model: str = "") -> bool:
        """Return True if this provider can transcribe audio."""
        return False
