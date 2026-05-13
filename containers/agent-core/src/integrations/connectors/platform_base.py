"""Base class for WASP platform bridge connectors.

Platform bridges connect to locally-running companion daemon processes via HTTP.
All actions are strictly allowlisted — no arbitrary command execution.
The connector is the HTTP client; the companion daemon runs separately on the target platform.

Bridge protocol:
    POST {bridge_url}/action/{action_name}
    Headers: Authorization: Bearer {api_key}, X-Bridge-Version: 1.0
    Body: JSON params
    Response: JSON {ok, data, error}
"""

from __future__ import annotations

from typing import Any

import httpx

from ..base import BaseConnector

_BRIDGE_TIMEOUT = 15.0


class PlatformBridgeConnector(BaseConnector):
    """Abstract base for local platform companion HTTP bridges.

    Subclasses define the manifest (with allowlisted actions) and delegate
    all execute() calls to _bridge_call(). No arbitrary command execution
    is permitted — the companion daemon enforces its own action allowlist.
    """

    BRIDGE_PROTOCOL_VERSION = "1.0"

    async def _bridge_call(
        self,
        bridge_url: str,
        api_key: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Send an action request to the local companion bridge daemon."""
        try:
            async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT) as c:
                r = await c.post(
                    f"{bridge_url.rstrip('/')}/action/{action}",
                    json=payload,
                    headers={
                        "Authorization":   f"Bearer {api_key}",
                        "X-Bridge-Version": self.BRIDGE_PROTOCOL_VERSION,
                        "Content-Type":    "application/json",
                    },
                )
                r.raise_for_status()
                return self.ok(r.json())
        except httpx.ConnectError:
            return self.err(
                f"Platform bridge not reachable at {bridge_url}. "
                "Is the companion daemon running?"
            )
        except httpx.HTTPStatusError as e:
            return self.err(
                f"Bridge returned HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:
            return self.err(str(e))

    async def health_check(self) -> bool:
        """Stateless — bridge reachability is checked at execute() time."""
        return True
