from __future__ import annotations
import json
import re
import time

import redis.asyncio as aioredis
import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()
EXEC_CTX_TTL = 3600


class ExtractFieldsSkill(SkillBase):
    """Extract fields from JSON and store in per-execution Redis context."""

    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="extract_fields",
            description=(
                "Extract fields from a JSON API response and store them as named variables. "
                "Use paths='[0].lastPrice:btc_price,[0].priceChangePercent:btc_change' syntax. "
                "After extraction, variables are available by name in subsequent skill calls. "
                "Supports array indexing ([0].field), nested objects (obj.field), and top-level fields."
            ),
            params=[
                SkillParam(name="json_text", param_type=ParamType.STRING,
                           description="Raw JSON string to extract from", required=False, default=""),
                SkillParam(name="source", param_type=ParamType.STRING,
                           description="Reference a previous step: 'step_3_output'", required=False, default=""),
                SkillParam(name="paths", param_type=ParamType.STRING,
                           description="Comma-separated path:var pairs. E.g. '[0].lastPrice:btc_price'"),
                SkillParam(name="execution_id", param_type=ParamType.STRING,
                           description="Execution context ID (auto-injected by capability executor)",
                           required=False, default=""),
            ],
            category="data",
            timeout_seconds=5.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        json_text = str(kwargs.get("json_text", "")).strip()
        source = str(kwargs.get("source", "")).strip()
        paths_str = str(kwargs.get("paths", "")).strip()
        execution_id = str(kwargs.get("execution_id", "")).strip()

        if not paths_str:
            return SkillResult(skill_name="extract_fields", success=False, output="", error="'paths' required")

        # Load from context if source= given
        if source and not json_text and execution_id:
            ctx_val = await self._ctx_get(execution_id, source)
            if ctx_val is None:
                return SkillResult(skill_name="extract_fields", success=False, output="",
                                   error=f"Source '{source}' not found in context {execution_id}")
            json_text = ctx_val

        if not json_text:
            return SkillResult(skill_name="extract_fields", success=False, output="",
                               error="Provide 'json_text' or 'source'+'execution_id'")

        # Parse JSON — strip HTTP prefix if present (output from http_request)
        data = None
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", json_text)
            if m:
                try:
                    data = json.loads(m.group(1))
                except Exception:
                    pass
        if data is None:
            return SkillResult(skill_name="extract_fields", success=False, output="", error="Cannot parse JSON")

        pair_re = re.compile(r"^(.*):([a-zA-Z_][a-zA-Z0-9_]*)$")
        extracted: dict[str, str] = {}
        errors: list[str] = []

        for pair in paths_str.split(","):
            pair = pair.strip()
            if not pair:
                continue
            m2 = pair_re.match(pair)
            if not m2:
                errors.append(f"Invalid pair '{pair}'")
                continue
            path, var_name = m2.group(1).strip(), m2.group(2).strip()
            try:
                value = _resolve_path(data, path)
                extracted[var_name] = str(value)
            except (KeyError, IndexError, TypeError) as exc:
                errors.append(f"Path '{path}': {exc}")

        if not extracted and errors:
            return SkillResult(skill_name="extract_fields", success=False, output="", error="; ".join(errors))

        if execution_id and extracted:
            await self._ctx_store_many(execution_id, extracted)

        lines = [f"{k} = {v}" for k, v in extracted.items()]
        if errors:
            lines.append(f"WARNINGS: {'; '.join(errors)}")
        return SkillResult(skill_name="extract_fields", success=True, output="\n".join(lines))

    async def _ctx_store_many(self, execution_id: str, data: dict[str, str]) -> None:
        key = f"exec:ctx:{execution_id}"
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.hset(key, mapping=data)
            await r.expire(key, EXEC_CTX_TTL)
        except Exception as exc:
            logger.debug("extract_fields.ctx_store_failed", error=str(exc)[:80])
        finally:
            await r.aclose()

    async def _ctx_get(self, execution_id: str, key: str) -> str | None:
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            return await r.hget(f"exec:ctx:{execution_id}", key)
        except Exception:
            return None
        finally:
            await r.aclose()


def _resolve_path(data, path: str):
    tokens = re.split(r"[\.\[\]]+", path.strip())
    tokens = [t for t in tokens if t]
    current = data
    for token in tokens:
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise TypeError(f"Cannot index {type(current).__name__} with '{token}'")
    return current
