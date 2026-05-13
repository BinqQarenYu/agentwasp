"""Integration Manager skill — lets the LLM configure integrations from chat.

Existing `IntegrationSkill` only *executes* integration actions (it needs the
integration to already be configured). This skill handles the configuration
side: listing what's available, inspecting what credentials are needed, storing
secrets in the vault, enabling/disabling, and (for Telegram specifically)
restarting the polling bridge so a freshly-set token takes effect.

Designed for natural-language flows like:
    User: "configure telegram, my token is 123:abc and my user id is 555"
    Agent: integration_manager(action="configure", id="telegram",
                               secrets={"bot_token":"123:abc","allowed_users":"555"})

Actions:
    list                       — list all integrations and whether they're configured
    describe id=<id>           — show required secrets, help text, and current status
    configure id=<id> secrets={k:v,...}  — store one or more secrets in the vault
    enable id=<id>             — enable an integration (after secrets set)
    disable id=<id>            — disable an integration
    set_secret id=<id> key=<k> value=<v>   — single secret (for tools that pass scalars)
    restart_bridge id=<id>     — restart polling container (only valid for 'telegram')
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

_SKILL_NAME = "integration_manager"

# Integrations that have a sidecar polling container which needs a restart when
# secrets change. Map: integration_id → docker container name.
_RESTART_TARGETS = {
    "telegram": "wasp-agent-telegram-1",
}


class IntegrationManagerSkill(SkillBase):
    """Configure and manage WASP integrations from chat."""

    def __init__(self, registry=None) -> None:
        # registry is the IntegrationRegistry. May be None at construction time
        # — late-wired from main.py after the registry is initialized.
        self._registry = registry

    def set_registry(self, registry) -> None:
        self._registry = registry

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=_SKILL_NAME,
            description=(
                "Configure WASP integrations (Telegram, Slack, GitHub, Notion, etc) from chat. "
                "Use this when the user asks to set up, configure, connect, enable, or disable "
                "an external service. NEVER guess secrets — ask the user for them first. "
                "Workflow: list → describe → configure → enable → (restart_bridge for telegram)."
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="One of: list, describe, configure, enable, disable, set_secret, restart_bridge",
                ),
                SkillParam(
                    name="id",
                    param_type=ParamType.STRING,
                    description="Integration ID (e.g. 'telegram', 'slack', 'github'). Required for all actions except 'list'.",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="secrets",
                    param_type=ParamType.STRING,
                    description='JSON object of {"key": "value"} pairs for the "configure" action. Example: {"bot_token":"123:abc","allowed_users":"555"}',
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="key",
                    param_type=ParamType.STRING,
                    description="Secret key name for the 'set_secret' action.",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="value",
                    param_type=ParamType.STRING,
                    description="Secret value for the 'set_secret' action.",
                    required=False,
                    default="",
                ),
            ],
            category="integrations",
            timeout_seconds=30.0,
        )

    async def execute(self, action: str = "", **kwargs: Any) -> SkillResult:
        action = (action or "").strip().lower()
        if not self._registry:
            return self._err("Integration registry not available (still initializing or disabled).")

        if action == "list":
            return await self._list()
        if action == "describe":
            return await self._describe(kwargs.get("id", "").strip())
        if action == "configure":
            return await self._configure(kwargs.get("id", "").strip(), kwargs.get("secrets", ""))
        if action == "set_secret":
            return await self._set_secret(
                kwargs.get("id", "").strip(),
                kwargs.get("key", "").strip(),
                kwargs.get("value", ""),
            )
        if action == "enable":
            return await self._enable(kwargs.get("id", "").strip())
        if action == "disable":
            return await self._disable(kwargs.get("id", "").strip())
        if action == "restart_bridge":
            return await self._restart_bridge(kwargs.get("id", "").strip())
        return self._err(
            f"Unknown action '{action}'. Valid: list, describe, configure, enable, disable, set_secret, restart_bridge"
        )

    # ── Actions ───────────────────────────────────────────────────────────

    async def _list(self) -> SkillResult:
        rows = self._registry.list_integrations()
        out_lines = []
        for r in rows:
            iid = r["id"]
            keys = await self._registry.vault.list_keys(iid)
            req = r.get("required_secrets") or []
            missing = [k for k in req if k not in keys]
            status = "✓ ready" if r["enabled"] and not missing else (
                "needs secrets: " + ", ".join(missing) if missing else "disabled"
            )
            out_lines.append(f"- {iid} ({r['category']}) — {status}")
        return SkillResult(
            skill_name=_SKILL_NAME, success=True,
            output="Available integrations:\n" + "\n".join(out_lines),
        )

    async def _describe(self, iid: str) -> SkillResult:
        if not iid:
            return self._err("id parameter is required for describe")
        try:
            m = self._registry.get_manifest(iid)
        except Exception:
            return self._err(f"Integration '{iid}' not found. Use action='list' to see available integrations.")
        configured_keys = await self._registry.vault.list_keys(iid)
        enabled = self._registry.policy.is_enabled(iid)
        specs = getattr(m, "secret_specs", None) or []
        if specs:
            secret_lines = []
            for s in specs:
                marker = "✓ set" if s.key in configured_keys else "✗ NOT SET"
                tag = "" if s.required else " (optional)"
                secret_lines.append(f"  - {s.key}{tag}: {s.label} [{marker}]")
                if s.help:
                    secret_lines.append(f"      ↳ {s.help}")
                if s.example:
                    secret_lines.append(f"      ↳ example: {s.example}")
        else:
            secret_lines = [
                f"  - {k}: {'✓ set' if k in configured_keys else '✗ NOT SET'}"
                for k in m.required_secrets
            ]
        body = (
            f"Integration: {m.name} ({iid})\n"
            f"Status: {'enabled' if enabled else 'disabled'}\n"
            f"Description: {m.description}\n"
            f"Required secrets:\n" + "\n".join(secret_lines)
        )
        return SkillResult(skill_name=_SKILL_NAME, success=True, output=body)

    async def _configure(self, iid: str, secrets_raw: Any) -> SkillResult:
        if not iid:
            return self._err("id parameter is required for configure")
        try:
            self._registry.get_manifest(iid)
        except Exception:
            return self._err(f"Integration '{iid}' not found. Use action='list' first.")

        if isinstance(secrets_raw, dict):
            secrets = secrets_raw
        elif isinstance(secrets_raw, str) and secrets_raw.strip():
            try:
                secrets = json.loads(secrets_raw)
            except Exception as e:
                return self._err(f"Invalid JSON in 'secrets' param: {e}. Expected an object like {{\"key\":\"value\"}}.")
        else:
            return self._err("secrets parameter is required and must be a JSON object of key/value pairs.")

        if not isinstance(secrets, dict) or not secrets:
            return self._err("secrets must be a non-empty object of key/value pairs.")

        saved = []
        for k, v in secrets.items():
            k = (k or "").strip()
            if not k:
                continue
            v_str = "" if v is None else str(v)
            if not v_str:
                continue
            await self._registry.vault.set(iid, k, v_str)
            saved.append(k)

        if not saved:
            return self._err("No secrets were saved — all values were empty.")

        # Convenience: auto-enable the integration once at least one secret is set
        enabled_now = ""
        if not self._registry.policy.is_enabled(iid):
            try:
                await self._registry.policy.enable(iid)
                enabled_now = " (and enabled)"
            except Exception as e:
                enabled_now = f" (enable failed: {e})"

        next_step = ""
        if iid in _RESTART_TARGETS:
            next_step = (
                f"\n\nTo activate the new {iid} configuration, restart the polling bridge: "
                f"call integration_manager(action='restart_bridge', id='{iid}')."
            )
        return SkillResult(
            skill_name=_SKILL_NAME, success=True,
            output=f"Saved {len(saved)} secret(s) for {iid}: {', '.join(saved)}{enabled_now}.{next_step}",
        )

    async def _set_secret(self, iid: str, key: str, value: str) -> SkillResult:
        if not iid or not key:
            return self._err("id and key parameters are required for set_secret")
        if not value:
            return self._err("value cannot be empty (use 'configure' with multiple keys, or pass a non-empty value)")
        try:
            self._registry.get_manifest(iid)
        except Exception:
            return self._err(f"Integration '{iid}' not found.")
        await self._registry.vault.set(iid, key, value)
        return SkillResult(
            skill_name=_SKILL_NAME, success=True,
            output=f"Saved secret '{key}' for {iid}.",
        )

    async def _enable(self, iid: str) -> SkillResult:
        if not iid:
            return self._err("id parameter is required for enable")
        try:
            self._registry.get_manifest(iid)
        except Exception:
            return self._err(f"Integration '{iid}' not found.")
        await self._registry.policy.enable(iid)
        return SkillResult(skill_name=_SKILL_NAME, success=True, output=f"Enabled {iid}.")

    async def _disable(self, iid: str) -> SkillResult:
        if not iid:
            return self._err("id parameter is required for disable")
        try:
            self._registry.get_manifest(iid)
        except Exception:
            return self._err(f"Integration '{iid}' not found.")
        await self._registry.policy.disable(iid)
        return SkillResult(skill_name=_SKILL_NAME, success=True, output=f"Disabled {iid}.")

    async def _restart_bridge(self, iid: str) -> SkillResult:
        """Restart a sidecar polling container so it picks up new secrets.

        We mirror vault → env for the bridge first (the polling bridge reads
        TELEGRAM_BOT_TOKEN from its environment, which is set from .env on the
        host). Then docker-restart the container via the mounted docker.sock.
        """
        if not iid:
            return self._err("id parameter is required for restart_bridge")
        container = _RESTART_TARGETS.get(iid)
        if not container:
            return self._err(f"Integration '{iid}' has no sidecar bridge to restart.")

        # Step 1: rewrite host .env so the bridge gets the new value on restart.
        # /opt/wasp/.env is mounted into agent-core as /data/host-env on dev
        # setups, but in default install it isn't mounted. Fall back to writing
        # a runtime-overlay file the bridge reads if present.
        import os
        env_targets = ["/opt/wasp/.env", "/host/etc/wasp.env", "/data/runtime.env"]
        env_written = ""
        if iid == "telegram":
            token = await self._registry.vault.get(iid, "bot_token") or ""
            allowed = await self._registry.vault.get(iid, "allowed_users") or ""
            updates = {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_ALLOWED_USERS": allowed,
            }
            for path in env_targets:
                if not os.path.exists(path):
                    continue
                try:
                    self._patch_env_file(path, updates)
                    env_written = path
                    break
                except Exception as e:
                    logger.warning("integration_manager.env_write_failed", path=path, error=str(e))

        # Step 2: restart the container via docker socket
        try:
            import httpx
            sock = "/var/run/docker.sock"
            if not os.path.exists(sock):
                return self._err(
                    f"Docker socket not available — cannot auto-restart {container}. "
                    f"Run manually on the host: docker restart {container}"
                )
            transport = httpx.HTTPTransport(uds=sock)
            with httpx.Client(transport=transport, base_url="http://docker", timeout=20.0) as client:
                r = client.post(f"/containers/{container}/restart")
            if r.status_code in (204, 304):
                hint = f" (.env updated: {env_written})" if env_written else ""
                return SkillResult(
                    skill_name=_SKILL_NAME, success=True,
                    output=f"Restarted container '{container}'{hint}. New {iid} secrets are now active.",
                )
            return self._err(f"Docker restart returned HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return self._err(f"Could not restart {container}: {e}. Run manually: docker restart {container}")

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _patch_env_file(path: str, updates: dict[str, str]) -> None:
        """Rewrite KEY=value lines in a .env file, appending any missing keys."""
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        seen: set[str] = set()
        out_lines = []
        for line in lines:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            if "=" not in stripped:
                out_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}\n")
                seen.add(key)
            else:
                out_lines.append(line)
        # Append any new keys that weren't already present
        for k, v in updates.items():
            if k not in seen:
                out_lines.append(f"{k}={v}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out_lines)

    @staticmethod
    def _err(msg: str) -> SkillResult:
        return SkillResult(skill_name=_SKILL_NAME, success=False, output=msg)
