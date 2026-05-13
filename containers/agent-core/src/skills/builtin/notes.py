from datetime import datetime, timezone

from ...db.session import async_session
from ...memory.manager import MemoryManager
from ...memory.types import MemoryQuery, MemoryType
from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult


class CreateNoteSkill(SkillBase):
    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="create_note",
            description="Create a note stored in semantic memory.",
            params=[
                SkillParam(name="title", param_type=ParamType.STRING, description="Note title"),
                SkillParam(name="content", param_type=ParamType.STRING, description="Note content"),
                SkillParam(name="tags", param_type=ParamType.STRING, description="Comma-separated tags", required=False, default=""),
            ],
            category="productivity",
        )

    async def execute(self, title: str, content: str, tags: str = "", **kwargs) -> SkillResult:
        try:
            tag_list = ["note"] + [t.strip() for t in tags.split(",") if t.strip()]
            async with async_session() as session:
                await self._memory.store_memory(
                    session,
                    memory_type=MemoryType.SEMANTIC,
                    content={"title": title, "body": content, "created_at": datetime.now(timezone.utc).isoformat()},
                    summary=f"Note: {title}",
                    tags=tag_list,
                )
            return SkillResult(skill_name="create_note", success=True, output=f"Note created: {title}")
        except Exception as e:
            return SkillResult(skill_name="create_note", success=False, output="", error=str(e))


class SearchNotesSkill(SkillBase):
    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="search_notes",
            description="Search notes by text.",
            params=[
                SkillParam(name="query", param_type=ParamType.STRING, description="Search text"),
            ],
            category="productivity",
        )

    async def execute(self, query: str, **kwargs) -> SkillResult:
        try:
            async with async_session() as session:
                entries = await self._memory.retrieve(
                    session,
                    MemoryQuery(memory_type=MemoryType.SEMANTIC, text_search=query, tags=["note"], limit=10),
                )
            if not entries:
                return SkillResult(skill_name="search_notes", success=True, output=f"No notes found for: {query}")

            lines = [f"Notes matching '{query}' ({len(entries)}):"]
            for entry in entries:
                title = entry.content.get("title", "untitled")
                body_preview = entry.content.get("body", "")[:80]
                lines.append(f"- {title}: {body_preview}")

            return SkillResult(skill_name="search_notes", success=True, output="\n".join(lines))
        except Exception as e:
            return SkillResult(skill_name="search_notes", success=False, output="", error=str(e))
