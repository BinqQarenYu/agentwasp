"""1Password read-only connector — returns references and metadata only, NEVER secret values."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 10.0
_REDACT_KEYS = {"value", "password", "secret", "credential", "token", "key", "pin", "passphrase"}


class OnePasswordConnector(BaseConnector):
    """1Password Connect Server (preferred) or op CLI (fallback).

    Security guarantee: NEVER returns field values. All responses are scanned
    and any key matching _REDACT_KEYS is replaced with '[REDACTED]'.
    """

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="1password",
            version="1.0.0",
            name="1Password",
            category="security",
            description="Read-only access to 1Password vaults. Returns references and metadata only — never exposes secret values.",
            capabilities=["read_vault_item", "list_vault_items", "get_item_reference"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="list_vaults",
                    description="List vault names and IDs (no item contents).",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="list_items",
                    description="List items in a vault: ID, title, category, tags. No field values.",
                    params=[
                        ParamSpec(name="vault_id", type="string", description="Vault UUID."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_item_metadata",
                    description="Get item metadata: URLs, tags, category, updated_at. No field values.",
                    params=[
                        ParamSpec(name="vault_id", type="string", description="Vault UUID."),
                        ParamSpec(name="item_id", type="string", description="Item UUID."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_item_reference",
                    description="Returns an op://VaultName/ItemTitle/FieldLabel URI. Does NOT call the API.",
                    params=[
                        ParamSpec(name="vault_name", type="string", description="Vault display name."),
                        ParamSpec(name="item_title", type="string", description="Item title."),
                        ParamSpec(name="field_label", type="string", description="Field label (e.g. 'password', 'username')."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="search_items",
                    description="Search items by title across vaults. Returns references only.",
                    params=[
                        ParamSpec(name="query", type="string", description="Search query."),
                        ParamSpec(name="vault_id", type="string", description="Limit search to this vault.", required=False),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["connect_host"],
            config_schema={},
            rate_limits={
                "list_vaults":        RateLimit(requests_per_minute=30),
                "list_items":         RateLimit(requests_per_minute=30),
                "get_item_metadata":  RateLimit(requests_per_minute=60),
                "get_item_reference": RateLimit(requests_per_minute=120),
                "search_items":       RateLimit(requests_per_minute=30),
            },
        )

    async def health_check(self) -> bool:
        return True  # Stateless; secrets passed at execute time

    def _redact_secret_fields(self, obj: Any) -> Any:
        """Recursively redact any dict key that matches _REDACT_KEYS."""
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k.lower() in _REDACT_KEYS:
                    result[k] = "[REDACTED]"
                else:
                    result[k] = self._redact_secret_fields(v)
            return result
        elif isinstance(obj, list):
            return [self._redact_secret_fields(item) for item in obj]
        return obj

    def _safe_item(self, item: dict) -> dict:
        """Strip 'fields' array and redact any remaining secret keys."""
        safe = {k: v for k, v in item.items() if k != "fields"}
        return self._redact_secret_fields(safe)

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        connect_host  = secrets.get("connect_host", "").rstrip("/")
        connect_token = secrets.get("connect_token", "")

        # get_item_reference: pure string construction, no API call
        if action == "get_item_reference":
            vault_name  = params.get("vault_name", "")
            item_title  = params.get("item_title", "")
            field_label = params.get("field_label", "")
            if not vault_name or not item_title or not field_label:
                return self.err("vault_name, item_title, and field_label are all required")
            ref = f"op://{vault_name}/{item_title}/{field_label}"
            return self.ok(ref)

        if not connect_host:
            return self.err("Secret 'connect_host' is required (1Password Connect Server URL)")

        headers: dict[str, str] = {}
        if connect_token:
            headers["Authorization"] = f"Bearer {connect_token}"

        try:
            async with httpx.AsyncClient(
                base_url=connect_host,
                headers=headers,
                timeout=_TIMEOUT,
            ) as client:

                if action == "list_vaults":
                    r = await client.get("/v1/vaults")
                    r.raise_for_status()
                    vaults = r.json()
                    safe = [{"id": v.get("id"), "name": v.get("name"), "items": v.get("items", 0)} for v in vaults]
                    return self.ok({"vaults": safe, "count": len(safe)})

                elif action == "list_items":
                    vault_id = params.get("vault_id", "")
                    if not vault_id:
                        return self.err("vault_id is required")
                    r = await client.get(f"/v1/vaults/{vault_id}/items")
                    r.raise_for_status()
                    items = r.json()
                    safe = [{"id": i.get("id"), "title": i.get("title"), "category": i.get("category"), "tags": i.get("tags", [])} for i in items]
                    return self.ok({"items": safe, "count": len(safe)})

                elif action == "get_item_metadata":
                    vault_id = params.get("vault_id", "")
                    item_id  = params.get("item_id", "")
                    if not vault_id or not item_id:
                        return self.err("vault_id and item_id are required")
                    r = await client.get(f"/v1/vaults/{vault_id}/items/{item_id}")
                    r.raise_for_status()
                    item = r.json()
                    metadata = {
                        "id":         item.get("id"),
                        "title":      item.get("title"),
                        "category":   item.get("category"),
                        "tags":       item.get("tags", []),
                        "urls":       item.get("urls", []),
                        "updated_at": item.get("updatedAt"),
                    }
                    return self.ok(self._redact_secret_fields(metadata))

                elif action == "search_items":
                    query    = params.get("query", "")
                    vault_id = params.get("vault_id")
                    if not query:
                        return self.err("query is required")
                    url = "/v1/items"
                    query_params: dict[str, str] = {"filter": f"title co \"{query}\""}
                    if vault_id:
                        query_params["vaultId"] = vault_id
                    r = await client.get(url, params=query_params)
                    r.raise_for_status()
                    items = r.json()
                    safe = [{"id": i.get("id"), "title": i.get("title"), "category": i.get("category")} for i in items]
                    return self.ok({"items": safe, "count": len(safe)})

                else:
                    return self.err(f"Unknown action: {action}")

        except httpx.HTTPStatusError as e:
            return self.err(f"1Password API error {e.response.status_code}: {e.response.text[:200]}")
        except httpx.ConnectError:
            return self.err(f"Cannot connect to 1Password Connect Server at {connect_host}")
        except Exception as e:
            return self.err(str(e))
