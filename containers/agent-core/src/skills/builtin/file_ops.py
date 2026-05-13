import os
from pathlib import Path

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

# ── Path sandbox ─────────────────────────────────────────────────────────────
# file_ops may ONLY access paths under these directories.
# This blocks reads/writes to /app (source code), /etc, /root, and all other
# system paths.  Self-improvement uses /data/src_patches exclusively.
_ALLOWED_BASE_DIRS: tuple[str, ...] = (
    "/data/memory",
    "/data/uploads",
    "/data/screenshots",
    "/data/shared",
    "/data/patches",
    "/data/src_patches",           # self-improve: persisted patches
    "/data/skills",                # custom python skill code
    "/data/backups",
    "/data/self_improve_backups",
    "/data/browser_sessions",
    "/data/chat-uploads",
    "/data/logs",
    "/tmp",
)


def _check_path(path: str) -> Path:
    """Resolve ``path`` and verify it falls inside an allowed base directory.

    Uses ``os.path.realpath`` to resolve symlinks and ``..`` traversal before
    the containment check — defeating path-traversal attacks like
    ``/data/memory/../../app/src/handlers.py``.

    Logs every file operation attempt (allowed or blocked) for audit visibility.

    Raises:
        ValueError: if the resolved path is outside all allowed bases.
    """
    resolved = os.path.realpath(path)
    for base in _ALLOWED_BASE_DIRS:
        if resolved == base or resolved.startswith(base + os.sep):
            logger.info(
                "file_ops.path_allowed",
                original_path=path,
                resolved_path=resolved,
                matched_base=base,
            )
            return Path(resolved)

    logger.warning(
        "file_ops.path_blocked",
        original_path=path,
        resolved_path=resolved,
        allowed_bases=list(_ALLOWED_BASE_DIRS),
    )
    raise ValueError(
        f"Path '{path}' resolves to '{resolved}' which is outside the allowed "
        f"directories.  Allowed bases: {', '.join(_ALLOWED_BASE_DIRS)}"
    )


class ReadFileSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="read_file",
            description=(
                "Read a file from allowed data directories (/data/memory, /data/uploads, "
                "/data/shared, /data/screenshots, /tmp, etc.).  "
                "Cannot access source code (/app) or system paths."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="File path (must be under /data/* or /tmp)"),
                SkillParam(name="max_lines", param_type=ParamType.INTEGER, description="Max lines", required=False, default="200"),
            ],
            category="filesystem",
            timeout_seconds=10.0,
        )

    async def execute(self, path: str, max_lines: str = "200", **kwargs) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="read_file", success=False, output="", error=str(e))

        try:
            if not p.exists():
                return SkillResult(skill_name="read_file", success=False, output="", error=f"File not found: {path}")
            if not p.is_file():
                return SkillResult(skill_name="read_file", success=False, output="", error=f"Not a file: {path}")

            max_l = min(int(max_lines), 500)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[:max_l]
            content = "\n".join(lines)
            return SkillResult(
                skill_name="read_file", success=True,
                output=f"File: {p} ({len(lines)} lines)\n---\n{content}",
            )
        except Exception as e:
            return SkillResult(skill_name="read_file", success=False, output="", error=str(e))


class WriteFileSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="write_file",
            description=(
                "Write content to a file in allowed data directories (/data/memory, /data/uploads, "
                "/data/shared, /data/patches, /tmp, etc.).  "
                "Cannot write to source code (/app) or system paths.  "
                "For self-improvement patches use /data/src_patches/ and apply_persisted_patches()."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="File path (must be under /data/* or /tmp)"),
                SkillParam(name="content", param_type=ParamType.STRING, description="Content to write"),
            ],
            category="filesystem",
            timeout_seconds=10.0,
        )

    async def execute(self, path: str, content: str, **kwargs) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="write_file", success=False, output="", error=str(e))

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            logger.info("file_ops.write_success", path=str(p), bytes=len(content.encode()))
            return SkillResult(
                skill_name="write_file", success=True,
                output=f"Written {len(content)} bytes to {p}",
            )
        except Exception as e:
            return SkillResult(skill_name="write_file", success=False, output="", error=str(e))
