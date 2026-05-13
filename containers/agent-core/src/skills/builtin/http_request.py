import json
import urllib.parse

import httpx

from ...utils.network_safety import validate_url_for_request
from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

MAX_RESPONSE_CHARS = 8000

# Project-specific block list (in addition to network_safety's defaults).
_PROJECT_BLOCKED_HOSTS = frozenset({
    "api.telegram.org",        # Use internal bus, not raw Telegram API
})


class HttpRequestSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="http_request",
            description="Make an HTTP request (GET, POST, PUT, DELETE, PATCH) and return the response.",
            params=[
                SkillParam(name="url", param_type=ParamType.STRING, description="Target URL"),
                SkillParam(
                    name="method",
                    param_type=ParamType.STRING,
                    description="HTTP method (GET, POST, PUT, DELETE, PATCH)",
                    required=False,
                    default="GET",
                ),
                SkillParam(
                    name="body",
                    param_type=ParamType.STRING,
                    description="Request body (for POST/PUT/PATCH)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="headers",
                    param_type=ParamType.STRING,
                    description='Headers as JSON, e.g. {"Authorization": "Bearer ..."}',
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="content_type",
                    param_type=ParamType.STRING,
                    description="Content-Type shortcut: json, form, text",
                    required=False,
                    default="json",
                ),
            ],
            category="web",
            timeout_seconds=30.0,
        )

    async def execute(
        self,
        url: str,
        method: str = "GET",
        body: str = "",
        headers: str = "",
        content_type: str = "json",
        **kwargs,
    ) -> SkillResult:
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            return SkillResult(
                skill_name="http_request",
                success=False,
                output="",
                error=f"Unsupported method: {method}",
            )

        # Project-specific host block (e.g. api.telegram.org — go via internal bus).
        try:
            _host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
        except Exception:
            _host = ""
        if _host in _PROJECT_BLOCKED_HOSTS:
            return SkillResult(
                skill_name="http_request",
                success=False,
                output="",
                error=f"Blocked: {_host} must be accessed via the internal bus",
            )
        # Full SSRF guard — resolves DNS, validates every A/AAAA record.
        _ssrf_reason = await validate_url_for_request(url)
        if _ssrf_reason is not None:
            return SkillResult(
                skill_name="http_request",
                success=False,
                output="",
                error=f"Blocked: {_ssrf_reason}",
            )

        try:
            req_headers = {"User-Agent": "AgentBot/1.0"}
            if headers:
                try:
                    custom = json.loads(headers)
                    if isinstance(custom, dict):
                        req_headers.update(custom)
                except json.JSONDecodeError:
                    pass

            ct_map = {
                "json": "application/json",
                "form": "application/x-www-form-urlencoded",
                "text": "text/plain",
            }
            if content_type in ct_map and body:
                req_headers.setdefault("Content-Type", ct_map[content_type])

            # follow_redirects=False — we revalidate each Location target ourselves
            # so a redirect to a private/metadata IP cannot bypass the SSRF guard.
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
                _current = url
                for _ in range(6):  # max 5 redirects
                    resp = await client.request(
                        method=method,
                        url=_current,
                        content=body.encode("utf-8") if body else None,
                        headers=req_headers,
                    )
                    if resp.is_redirect and resp.headers.get("location"):
                        _current = str(httpx.URL(_current).join(resp.headers["location"]))
                        _r = await validate_url_for_request(_current)
                        if _r is not None:
                            return SkillResult(
                                skill_name="http_request",
                                success=False,
                                output="",
                                error=f"Blocked redirect: {_r}",
                            )
                        # For non-GET, RFC says client may downgrade to GET on 301/302/303.
                        # httpx default behavior is the same; we just refetch with same method.
                        continue
                    break

            resp_body = resp.text
            if len(resp_body) > MAX_RESPONSE_CHARS:
                resp_body = resp_body[:MAX_RESPONSE_CHARS] + f"\n... (truncated, {len(resp.text)} total chars)"

            output = f"HTTP {resp.status_code}\n"
            for h in ("Content-Type", "Location", "X-Request-Id"):
                if h in resp.headers:
                    output += f"{h}: {resp.headers[h]}\n"
            output += f"\n{resp_body}"

            return SkillResult(
                skill_name="http_request",
                success=200 <= resp.status_code < 400,
                output=output.strip(),
                error="" if 200 <= resp.status_code < 400 else f"HTTP {resp.status_code}",
            )
        except Exception as e:
            return SkillResult(
                skill_name="http_request", success=False, output="", error=str(e),
            )
