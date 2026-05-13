from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str  # "system", "user", "assistant"
    content: str
    # Internal metadata (NOT sent to LLM providers — see manager.py:45 which
    # only forwards role+content). Used by the policy.intent_gate to skip
    # few-shot examples that would otherwise look like real user content.
    meta: dict = Field(default_factory=dict)


class ModelRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 1024
    # Multimodal: set one of these when sending media
    image_path: str | None = None   # absolute path to image file
    audio_path: str | None = None   # absolute path to audio file (ogg/mp3/wav)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelResponse(BaseModel):
    content: str
    model_used: str = ""
    provider: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
