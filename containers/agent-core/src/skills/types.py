from enum import Enum

from pydantic import BaseModel, Field


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"


class SkillParam(BaseModel):
    name: str
    param_type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    default: str | None = None


class SkillDefinition(BaseModel):
    name: str
    description: str
    params: list[SkillParam] = Field(default_factory=list)
    category: str = "general"
    requires_confirmation: bool = False
    enabled: bool = True
    timeout_seconds: float = 30.0
    cooldown_seconds: float = 0.0
    # Capability level for policy enforcement and audit tagging.
    # Import string to avoid circular dependency; resolved at runtime.
    capability_level: str = "controlled"  # safe|monitored|controlled|restricted|privileged


class SkillCall(BaseModel):
    skill_name: str
    arguments: dict[str, str] = Field(default_factory=dict)
    raw_text: str = ""
    parallel_group: int | None = None  # skills sharing same group run concurrently


class SkillResult(BaseModel):
    skill_name: str
    success: bool
    output: str
    error: str = ""
    execution_ms: int = 0
