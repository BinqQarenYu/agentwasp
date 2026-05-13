from abc import ABC, abstractmethod

from .types import SkillDefinition, SkillResult


class SkillBase(ABC):
    """Abstract base class for all skills."""

    @abstractmethod
    def definition(self) -> SkillDefinition:
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> SkillResult:
        ...
