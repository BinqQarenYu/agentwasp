"""Pluggable embedding provider abstraction for WASP vector memory.

Factory usage:
    from src.memory.embeddings import create_provider
    provider = create_provider(settings)
    vec = await provider.embed("some text")

Provider selection (settings.embedding_provider):
    "ollama"  — Ollama local inference (default)
    "openai"  — OpenAI text-embedding-* (requires openai_api_key)
    "hash"    — deterministic hash fallback (non-semantic, always available)
    "auto"    — try Ollama first, then OpenAI, then hash
"""
from __future__ import annotations

from .base import EmbeddingProvider
from .hash_fallback import HashFallbackEmbeddingProvider
from .ollama import OllamaEmbeddingProvider
from .openai import OpenAIEmbeddingProvider

__all__ = [
    "EmbeddingProvider",
    "OllamaEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "HashFallbackEmbeddingProvider",
    "create_provider",
]


def create_provider(settings) -> EmbeddingProvider:  # type: ignore[type-arg]
    """Build the appropriate EmbeddingProvider from runtime settings.

    Falls back toward hash_fallback when credentials are absent.
    """
    import structlog
    log = structlog.get_logger()

    ep = getattr(settings, "embedding_provider", "ollama").lower()

    if ep == "openai":
        key = getattr(settings, "openai_api_key", "") or ""
        if key:
            log.info("embeddings.provider_selected", provider="openai",
                     model=getattr(settings, "vector_embed_model", "text-embedding-3-small"))
            return OpenAIEmbeddingProvider(
                api_key=key,
                model=getattr(settings, "vector_embed_model", "text-embedding-3-small"),
            )
        log.warning("embeddings.openai_key_missing", fallback="ollama")
        ep = "ollama"

    if ep in ("ollama", "auto"):
        ollama_url = getattr(settings, "ollama_base_url", "http://agent-ollama:11434")
        model = getattr(settings, "vector_embed_model", "nomic-embed-text")
        log.info("embeddings.provider_selected", provider="ollama", model=model)
        return OllamaEmbeddingProvider(ollama_url=ollama_url, model=model)

    # "hash" or unrecognised — explicit non-semantic mode
    log.info("embeddings.provider_selected", provider="hash_fallback")
    return HashFallbackEmbeddingProvider()
