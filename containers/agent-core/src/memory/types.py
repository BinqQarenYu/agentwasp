from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    FACTS = "facts"
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    POLICY = "policy"
    META = "meta"


class MemoryContent(BaseModel):
    """The content stored in a memory JSON file."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    memory_type: MemoryType
    project_id: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    version: int = 1
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    content: dict = Field(default_factory=dict)
    # Lifecycle fields
    expires_at: str | None = None          # ISO timestamp; None = never expires
    importance_score: float = 0.5          # 0.0 (irrelevant) → 1.0 (critical)
    mention_count: int = 0                 # Times this topic appeared in episodic
    source: str = "conversation"           # conversation|reflection|auto|seed|manual


class MemoryQuery(BaseModel):
    """Query parameters for searching memory."""
    memory_type: MemoryType | None = None
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    text_search: str = ""
    limit: int = 10
    offset: int = 0


class SnapshotInfo(BaseModel):
    id: str
    label: str
    created_at: str
    entry_count: int
    size_bytes: int
    trigger: str = "manual"
