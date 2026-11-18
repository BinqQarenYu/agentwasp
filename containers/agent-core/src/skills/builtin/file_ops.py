"""File operations skill — Antigravity-grade filesystem workbench.

Provides:
  - read_file(path, start_line, max_lines): read file with line numbers & range slicing
  - write_file(path, content, overwrite, append): create or append to file
  - edit_file(path, target, replacement, count): surgical in-place replacement
  - list_dir(path, max_depth, show_hidden): structured directory listing
  - search_files(path, pattern, recursive, max_matches): regex/text search across files
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

# Allowed base paths for file operations (container & host mapped workspaces)
_ALLOWED_BASE_DIRS: tuple[str, ...] = (
    "/data",
    "/tmp",
    "/app",
    "/mnt/f_drive",
    "/workspace",
)


def _check_path(path: str) -> Path:
    """Resolve path and verify it falls inside allowed directories.

    Handles container Linux paths (/app, /data, /mnt/f_drive, /tmp) and
    local host paths (e.g. F:\\012A_Github\\agentwasp or relative paths in tests).
    """
    p_str = str(path).strip()
    if not p_str:
        raise ValueError("File path cannot be empty")

    resolved = os.path.realpath(os.path.abspath(p_str))
    norm_resolved = resolved.replace("\\", "/").lower()

    # Host-side repo root detection (e.g. F:/012A_Github/agentwasp or current working dir)
    cwd_norm = os.path.realpath(os.getcwd()).replace("\\", "/").lower()

    # Check container base dirs
    for base in _ALLOWED_BASE_DIRS:
        b_norm = base.lower()
        if norm_resolved == b_norm or norm_resolved.startswith(b_norm + "/"):
            return Path(resolved)

    # Check local repo workspace (when running tests or locally outside docker)
    if norm_resolved == cwd_norm or norm_resolved.startswith(cwd_norm + "/"):
        return Path(resolved)

    # Check drive mount equivalents (e.g. F:/... matches /mnt/f_drive/...)
    if norm_resolved.startswith("f:/") or norm_resolved.startswith("/mnt/f/"):
        return Path(resolved)

    # Check temp dir
    import tempfile
    temp_norm = os.path.realpath(tempfile.gettempdir()).replace("\\", "/").lower()
    if norm_resolved == temp_norm or norm_resolved.startswith(temp_norm + "/"):
        return Path(resolved)

    logger.warning("file_ops.path_blocked", original_path=path, resolved_path=resolved)
    raise ValueError(
        f"Path '{path}' resolves to '{resolved}' which is outside allowed workspaces: "
        f"{', '.join(_ALLOWED_BASE_DIRS)}"
    )


class ReadFileSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="read_file",
            description=(
                "Read file content with optional line-range slicing and line numbers. "
                "Works across /mnt/f_drive/*, /app/src/*, /data/*, and /tmp."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="Absolute or workspace-relative path"),
                SkillParam(name="start_line", param_type=ParamType.INTEGER, description="1-indexed line to start reading from", required=False, default="1"),
                SkillParam(name="max_lines", param_type=ParamType.INTEGER, description="Maximum number of lines to return", required=False, default="200"),
            ],
            category="filesystem",
            timeout_seconds=15.0,
        )

    async def execute(self, path: str, start_line: str = "1", max_lines: str = "200", **kwargs) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="read_file", success=False, output="", error=str(e))

        try:
            if not p.exists():
                return SkillResult(skill_name="read_file", success=False, output="", error=f"File not found: {path}")
            if not p.is_file():
                return SkillResult(skill_name="read_file", success=False, output="", error=f"Path is not a file: {path}")

            try:
                s_line = max(1, int(start_line))
            except (ValueError, TypeError):
                s_line = 1

            try:
                m_lines = max(1, min(1000, int(max_lines)))
            except (ValueError, TypeError):
                m_lines = 200

            all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            total_lines = len(all_lines)

            # 1-indexed slice
            start_idx = s_line - 1
            end_idx = min(total_lines, start_idx + m_lines)
            selected = all_lines[start_idx:end_idx]

            numbered = [f"{start_idx + i + 1:5d}: {line}" for i, line in enumerate(selected)]
            header = f"File: {p} (Showing lines {start_idx + 1}-{end_idx} of {total_lines})"
            output = f"{header}\n" + "-" * len(header) + "\n" + "\n".join(numbered)

            return SkillResult(skill_name="read_file", success=True, output=output)
        except Exception as e:
            logger.exception("read_file.failed", path=path)
            return SkillResult(skill_name="read_file", success=False, output="", error=str(e))


class WriteFileSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="write_file",
            description=(
                "Create a file or append to an existing file with the provided content. "
                "Automatically creates parent directories. Set overwrite=true to replace existing files."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="File path to write"),
                SkillParam(name="content", param_type=ParamType.STRING, description="Full string content to write"),
                SkillParam(name="overwrite", param_type=ParamType.BOOLEAN, description="Allow overwriting existing file", required=False, default="false"),
                SkillParam(name="append", param_type=ParamType.BOOLEAN, description="Append content to existing file", required=False, default="false"),
            ],
            category="filesystem",
            timeout_seconds=15.0,
        )

    async def execute(
        self,
        path: str,
        content: str,
        overwrite: str | bool = "false",
        append: str | bool = "false",
        **kwargs,
    ) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="write_file", success=False, output="", error=str(e))

        try:
            is_overwrite = str(overwrite).lower() in ("true", "1", "yes")
            is_append = str(append).lower() in ("true", "1", "yes")

            if p.exists() and not is_overwrite and not is_append:
                return SkillResult(
                    skill_name="write_file",
                    success=False,
                    output="",
                    error=f"File already exists: {path}. Set overwrite=true to replace or append=true to append.",
                )

            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if is_append else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(content)

            verb = "Appended" if is_append else "Wrote"
            size = p.stat().st_size
            return SkillResult(
                skill_name="write_file",
                success=True,
                output=f"{verb} {len(content)} chars to {p} (Total size: {size} bytes)",
            )
        except Exception as e:
            logger.exception("write_file.failed", path=path)
            return SkillResult(skill_name="write_file", success=False, output="", error=str(e))


class EditFileSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="edit_file",
            description=(
                "Perform surgical string replacement in a file. Matches target exact text and replaces with replacement. "
                "Fails if target is not found in file."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="File path to edit"),
                SkillParam(name="target", param_type=ParamType.STRING, description="Exact text to find and replace"),
                SkillParam(name="replacement", param_type=ParamType.STRING, description="New text to substitute in"),
                SkillParam(name="count", param_type=ParamType.INTEGER, description="Occurrences to replace (default 1, -1 for all)", required=False, default="1"),
            ],
            category="filesystem",
            timeout_seconds=15.0,
        )

    async def execute(
        self,
        path: str,
        target: str,
        replacement: str,
        count: str = "1",
        **kwargs,
    ) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="edit_file", success=False, output="", error=str(e))

        try:
            if not p.exists() or not p.is_file():
                return SkillResult(skill_name="edit_file", success=False, output="", error=f"File not found: {path}")

            try:
                rep_count = int(count)
            except (ValueError, TypeError):
                rep_count = 1

            original = p.read_text(encoding="utf-8", errors="replace")
            if target not in original:
                return SkillResult(
                    skill_name="edit_file",
                    success=False,
                    output="",
                    error=f"Target text not found in {path}. Make sure whitespace and indentation match exactly.",
                )

            occurrences = original.count(target)
            if rep_count == -1:
                updated = original.replace(target, replacement)
                replaced_count = occurrences
            else:
                updated = original.replace(target, replacement, rep_count)
                replaced_count = min(occurrences, rep_count)

            p.write_text(updated, encoding="utf-8")
            return SkillResult(
                skill_name="edit_file",
                success=True,
                output=f"Successfully replaced {replaced_count} occurrence(s) in {p}",
            )
        except Exception as e:
            logger.exception("edit_file.failed", path=path)
            return SkillResult(skill_name="edit_file", success=False, output="", error=str(e))


class ListDirSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="list_dir",
            description=(
                "List directory contents with file types, sizes, and nested structures. "
                "Works across /mnt/f_drive/*, /app/src/*, /data/*, and /tmp."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="Directory path to inspect"),
                SkillParam(name="max_depth", param_type=ParamType.INTEGER, description="Max subdirectory recursion depth (1-3)", required=False, default="1"),
                SkillParam(name="show_hidden", param_type=ParamType.BOOLEAN, description="Include dotfiles/hidden files", required=False, default="false"),
            ],
            category="filesystem",
            timeout_seconds=15.0,
        )

    async def execute(
        self,
        path: str,
        max_depth: str = "1",
        show_hidden: str | bool = "false",
        **kwargs,
    ) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="list_dir", success=False, output="", error=str(e))

        try:
            if not p.exists() or not p.is_dir():
                return SkillResult(skill_name="list_dir", success=False, output="", error=f"Directory not found: {path}")

            try:
                depth = max(1, min(3, int(max_depth)))
            except (ValueError, TypeError):
                depth = 1

            include_hidden = str(show_hidden).lower() in ("true", "1", "yes")

            entries_out = []

            def _format_size(size_bytes: int) -> str:
                if size_bytes < 1024:
                    return f"{size_bytes}B"
                elif size_bytes < 1024 * 1024:
                    return f"{size_bytes / 1024:.1f}KB"
                return f"{size_bytes / (1024 * 1024):.1f}MB"

            def _scan(current_dir: Path, current_depth: int, prefix: str = ""):
                try:
                    items = sorted(current_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                except PermissionError:
                    entries_out.append(f"{prefix}[Permission Denied]")
                    return

                for item in items:
                    if not include_hidden and item.name.startswith("."):
                        continue
                    if item.is_dir():
                        entries_out.append(f"{prefix}📁 {item.name}/")
                        if current_depth < depth:
                            _scan(item, current_depth + 1, prefix + "  ")
                    else:
                        try:
                            sz = _format_size(item.stat().st_size)
                        except Exception:
                            sz = "unknown"
                        entries_out.append(f"{prefix}📄 {item.name} ({sz})")

            _scan(p, 1)
            output = f"Directory: {p} ({len(entries_out)} items)\n" + "\n".join(entries_out[:200])
            if len(entries_out) > 200:
                output += f"\n... [Truncated: {len(entries_out) - 200} more items]"

            return SkillResult(skill_name="list_dir", success=True, output=output)
        except Exception as e:
            logger.exception("list_dir.failed", path=path)
            return SkillResult(skill_name="list_dir", success=False, output="", error=str(e))


class SearchFilesSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="search_files",
            description=(
                "Search for regular expressions or plain text across files in a directory tree. "
                "Returns matching paths, line numbers, and line contents."
            ),
            params=[
                SkillParam(name="path", param_type=ParamType.STRING, description="Directory or file path to search"),
                SkillParam(name="pattern", param_type=ParamType.STRING, description="Regex pattern or search substring"),
                SkillParam(name="recursive", param_type=ParamType.BOOLEAN, description="Search subdirectories recursively", required=False, default="true"),
                SkillParam(name="max_matches", param_type=ParamType.INTEGER, description="Maximum matches to return", required=False, default="50"),
            ],
            category="filesystem",
            timeout_seconds=20.0,
        )

    async def execute(
        self,
        path: str,
        pattern: str,
        recursive: str | bool = "true",
        max_matches: str = "50",
        **kwargs,
    ) -> SkillResult:
        try:
            p = _check_path(path)
        except ValueError as e:
            return SkillResult(skill_name="search_files", success=False, output="", error=str(e))

        try:
            if not p.exists():
                return SkillResult(skill_name="search_files", success=False, output="", error=f"Path not found: {path}")

            is_recursive = str(recursive).lower() in ("true", "1", "yes")
            try:
                limit = max(1, min(200, int(max_matches)))
            except (ValueError, TypeError):
                limit = 50

            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return SkillResult(skill_name="search_files", success=False, output="", error=f"Invalid regex: {e}")

            files_to_check: list[Path] = []
            if p.is_file():
                files_to_check.append(p)
            elif is_recursive:
                for root, _, files in os.walk(p):
                    for f in files:
                        files_to_check.append(Path(root) / f)
            else:
                files_to_check = [item for item in p.iterdir() if item.is_file()]

            matches = []
            for file_path in files_to_check:
                # Skip binary, logs, and venv/git dirs
                str_p = str(file_path).replace("\\", "/")
                if any(ignored in str_p for ignored in ("/.git/", "/__pycache__/", "/.venv/", "/node_modules/")):
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, start=1):
                            if regex.search(line):
                                matches.append(f"{file_path}:{line_num}: {line.strip()[:180]}")
                                if len(matches) >= limit:
                                    break
                except Exception:
                    continue
                if len(matches) >= limit:
                    break

            if not matches:
                return SkillResult(
                    skill_name="search_files",
                    success=True,
                    output=f"No matches found for '{pattern}' in {p}",
                )

            output = f"Found {len(matches)} match(es) for '{pattern}':\n" + "\n".join(matches)
            return SkillResult(skill_name="search_files", success=True, output=output)
        except Exception as e:
            logger.exception("search_files.failed", path=path)
            return SkillResult(skill_name="search_files", success=False, output="", error=str(e))
