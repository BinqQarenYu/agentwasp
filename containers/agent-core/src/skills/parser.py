import re

from .types import SkillCall

# Canonical format: <skill>name(args)</skill>
SKILL_PATTERN = re.compile(
    r"<skill>\s*(\w+)\s*\((.*?)\)\s*</skill>",
    re.DOTALL,
)

# Parallel block: <parallel>...<skill>...</skill>...<skill>...</skill>...</parallel>
PARALLEL_PATTERN = re.compile(
    r"<parallel>(.*?)</parallel>",
    re.DOTALL,
)

# Fallback formats small models produce:
# <name(args)>  |  [skill: name(args)]  |  ```skill\nname(args)```  |  plain name(args) on its own line
_FALLBACK_PATTERNS = [
    re.compile(r"<(\w+)\s*\((.*?)\)\s*>", re.DOTALL),
    re.compile(r"\[skill:\s*(\w+)\s*\((.*?)\)\s*\]", re.DOTALL),
    # Plain function-call format: skill_name(args) on its own line (validated against _KNOWN_SKILLS)
    re.compile(r"(?m)^[ \t]*(\w+)\(([^)]*)\)[ \t]*$"),
]

ARG_PATTERN = re.compile(
    r"""(\w+)\s*=\s*(?:\"\"\"(.*?)\"\"\"|\'\'\'(.*?)\'\'\'|"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)'|(\S+))""",
    re.DOTALL,
)


def _parse_args(args_str: str) -> dict:  # type: ignore[override]  # redefined below
    arguments = {}
    for arg_match in ARG_PATTERN.finditer(args_str):
        key = arg_match.group(1)
        # Groups: 2=triple-double, 3=triple-single, 4=double-quoted, 5=single-quoted, 6=unquoted
        value = (
            arg_match.group(2)
            or arg_match.group(3)
            or arg_match.group(4)
            or arg_match.group(5)
            or arg_match.group(6)
            or ""
        )
        # Unescape backslash sequences in single/double quoted strings
        if arg_match.group(4) or arg_match.group(5):
            value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")
        arguments[key] = value
    return arguments

# Known skill names for fallback validation
_KNOWN_SKILLS = {
    "create_reminder", "list_reminders", "create_note", "search_notes",
    "get_datetime", "get_weather", "web_search", "web_read", "fetch_url",
    "calculate", "translate", "system_info", "read_file", "write_file",
    "shell", "python_exec", "http_request", "browser", "openclaw",
    "create_monitor", "list_monitors", "remove_monitor",
    "subscribe", "self_improve", "skill_manager", "task_manager",
    "gmail", "delete_reminder", "agent_manager", "render_report", "extract_fields",
}


def parse_skill_calls(text: str) -> list[SkillCall]:
    """Parse skill calls from LLM output. Handles canonical, parallel, and fallback formats.

    Parallel format:
        <parallel>
          <skill>fetch_url(url="...")</skill>
          <skill>fetch_url(url="...")</skill>
        </parallel>
    Skills inside <parallel> get the same parallel_group id and are executed concurrently.
    """
    calls: list[SkillCall] = []
    seen_raw: set[str] = set()
    consumed_spans: list[tuple[int, int]] = []  # track text ranges consumed by parallel blocks

    # --- Extract parallel blocks first ---
    group_id = 0
    for par_match in PARALLEL_PATTERN.finditer(text):
        group_id += 1
        consumed_spans.append((par_match.start(), par_match.end()))
        inner = par_match.group(1)
        for match in SKILL_PATTERN.finditer(inner):
            skill_name = match.group(1).strip().lower()
            raw_text = match.group(0)
            if raw_text not in seen_raw:
                calls.append(SkillCall(
                    skill_name=skill_name,
                    arguments=_parse_args(match.group(2).strip()),
                    raw_text=raw_text,
                    parallel_group=group_id,
                ))
                seen_raw.add(raw_text)

    # --- Extract sequential skills (outside parallel blocks) ---
    for match in SKILL_PATTERN.finditer(text):
        # Skip if this match is inside a parallel block
        inside_parallel = any(s <= match.start() < e for s, e in consumed_spans)
        if inside_parallel:
            continue
        raw_text = match.group(0)
        if raw_text not in seen_raw:
            calls.append(SkillCall(
                skill_name=match.group(1).strip().lower(),
                arguments=_parse_args(match.group(2).strip()),
                raw_text=raw_text,
                parallel_group=None,
            ))
            seen_raw.add(raw_text)

    # --- Fallback patterns (only when nothing canonical found) ---
    if not calls:
        for pattern in _FALLBACK_PATTERNS:
            for match in pattern.finditer(text):
                skill_name = match.group(1).strip().lower()
                raw_text = match.group(0)
                if skill_name in _KNOWN_SKILLS and raw_text not in seen_raw:
                    calls.append(SkillCall(
                        skill_name=skill_name,
                        arguments=_parse_args(match.group(2).strip()),
                        raw_text=raw_text,
                    ))
                    seen_raw.add(raw_text)

    return calls


_ALL_PATTERNS = [SKILL_PATTERN, PARALLEL_PATTERN] + _FALLBACK_PATTERNS


def strip_skill_calls(text: str) -> str:
    """Remove all skill call blocks (including parallel wrappers) from text."""
    result = text
    for pattern in _ALL_PATTERNS:
        result = pattern.sub("", result)
    return result.strip()
