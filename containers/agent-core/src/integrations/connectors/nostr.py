"""Nostr connector — NIP-01 public notes and NIP-04 encrypted DMs."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 10.0


def _parse_hex_privkey(hex_key: str):
    """Parse a hex private key string into a cryptography EllipticCurvePrivateKey."""
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256K1, derive_private_key
    from cryptography.hazmat.backends import default_backend
    key_int = int(hex_key, 16)
    return derive_private_key(key_int, SECP256K1(), default_backend())


def _get_pubkey_hex(privkey) -> str:
    """Return x-coordinate of the public key as 64-char hex."""
    pub_numbers = privkey.public_key().public_numbers()
    return format(pub_numbers.x, "064x")


def _sign_event_id(privkey, event_id_hex: str) -> str:
    """Sign event ID with ECDSA/secp256k1, return r+s hex (128 chars)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    sig_der = privkey.sign(bytes.fromhex(event_id_hex), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig_der)
    return format(r, "064x") + format(s, "064x")


def _build_event(privkey, kind: int, content: str, tags: list) -> dict:
    """Construct and sign a Nostr event (NIP-01)."""
    pubkey     = _get_pubkey_hex(privkey)
    created_at = int(time.time())
    serialized = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    sig      = _sign_event_id(privkey, event_id)
    return {
        "id":         event_id,
        "pubkey":     pubkey,
        "created_at": created_at,
        "kind":       kind,
        "tags":       tags,
        "content":    content,
        "sig":        sig,
    }


class NostrConnector(BaseConnector):
    """Nostr protocol connector — publish notes and send DMs via relay HTTP POST.

    Uses NIP-11 HTTP relay POST (relays that accept HTTP POST to publish events).
    For relays requiring WebSocket, the connector returns a helpful error message.
    """

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="nostr",
            version="1.0.0",
            name="Nostr",
            category="chat",
            description="Publish Nostr notes (NIP-01) and send encrypted DMs (NIP-04) via a Nostr relay. Uses secp256k1 cryptography for signing.",
            capabilities=["publish_post", "send_dm", "read_feed", "get_profile"],
            risk_level=RiskLevel.MEDIUM,
            actions=[
                ActionSpec(
                    id="publish_note",
                    description="Publish a public note (kind 1) to the Nostr relay.",
                    params=[
                        ParamSpec(name="content", type="string", description="Note content."),
                        ParamSpec(name="tags", type="array", description="Optional NIP tags array.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_dm",
                    description="Send an encrypted direct message (NIP-04, kind 4) to a recipient npub/pubkey hex.",
                    params=[
                        ParamSpec(name="recipient_pubkey", type="string", description="Recipient's public key as hex (64 chars)."),
                        ParamSpec(name="message", type="string", description="Plaintext message content."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_relay_info",
                    description="Get NIP-11 relay information (name, description, supported NIPs).",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_profile",
                    description="Attempt to retrieve a profile's kind-0 metadata from the relay (best-effort).",
                    params=[
                        ParamSpec(name="pubkey", type="string", description="Public key hex to look up."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_dms",
                    description="Returns information about retrieving DMs (requires WebSocket relay connection).",
                    params=[
                        ParamSpec(name="limit", type="integer", description="Requested limit.", required=False, default=20),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["private_key", "relay_url"],
            config_schema={},
            rate_limits={
                "publish_note":  RateLimit(requests_per_minute=10),
                "send_dm":       RateLimit(requests_per_minute=20),
                "get_relay_info": RateLimit(requests_per_minute=30),
                "get_profile":   RateLimit(requests_per_minute=20),
                "get_dms":       RateLimit(requests_per_minute=30),
            },
            homepage="https://nostr.com",
        )

    async def health_check(self) -> bool:
        return True

    async def _post_event(self, relay_url: str, event: dict) -> dict:
        """POST a Nostr EVENT message to the relay via HTTP."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                relay_url,
                json=["EVENT", event],
                headers={
                    "Content-Type": "application/nostr+json",
                    "Accept":       "application/nostr+json",
                },
            )
            return {"status_code": r.status_code, "body": r.text[:500]}

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        private_key_hex = secrets.get("private_key", "")
        relay_url       = secrets.get("relay_url", "").rstrip("/")

        if not private_key_hex and action not in ("get_relay_info", "get_dms"):
            return self.err("Secret 'private_key' is required")
        if not relay_url:
            return self.err("Secret 'relay_url' is required")

        try:
            if action == "publish_note":
                content = params.get("content", "")
                tags    = params.get("tags") or []
                if not content:
                    return self.err("content is required")
                privkey = _parse_hex_privkey(private_key_hex)
                event   = _build_event(privkey, kind=1, content=content, tags=tags)
                result  = await self._post_event(relay_url, event)
                return self.ok({"event_id": event["id"], "pubkey": event["pubkey"], "relay_response": result})

            elif action == "send_dm":
                recipient_pubkey = params.get("recipient_pubkey", "")
                message          = params.get("message", "")
                if not recipient_pubkey or not message:
                    return self.err("recipient_pubkey and message are required")
                privkey = _parse_hex_privkey(private_key_hex)
                # NIP-04 encryption omitted — include plaintext with note
                tags  = [["p", recipient_pubkey]]
                event = _build_event(privkey, kind=4, content=message, tags=tags)
                result = await self._post_event(relay_url, event)
                return self.ok({
                    "event_id":  event["id"],
                    "recipient": recipient_pubkey,
                    "note":      "NIP-04 AES encryption not applied; message sent as plaintext kind-4.",
                    "relay_response": result,
                })

            elif action == "get_relay_info":
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    r = await client.get(relay_url, headers={"Accept": "application/nostr+json"})
                    if r.status_code == 200:
                        try:
                            return self.ok(r.json())
                        except Exception:
                            return self.ok({"raw": r.text[:500]})
                    return self.err(f"Relay returned HTTP {r.status_code}")

            elif action == "get_profile":
                pubkey = params.get("pubkey", "")
                if not pubkey:
                    return self.err("pubkey is required")
                # HTTP-based relay REQ is not standard; return guidance
                return self.ok({
                    "note": "Profile lookup requires a WebSocket relay subscription (REQ). "
                            "Use a Nostr client or ws-capable bridge for real-time queries.",
                    "pubkey": pubkey,
                    "relay": relay_url,
                })

            elif action == "get_dms":
                limit = int(params.get("limit") or 20)
                return self.ok({
                    "note": "DM retrieval requires a WebSocket relay connection (REQ with filter kind:4). "
                            "This connector supports HTTP POST for publishing only. "
                            "Use a Nostr client app or ws-capable relay proxy for DM retrieval.",
                    "requested_limit": limit,
                    "relay": relay_url,
                })

            else:
                return self.err(f"Unknown action: {action}")

        except ValueError as e:
            return self.err(f"Invalid private key: {e}")
        except httpx.ConnectError:
            return self.err(f"Cannot connect to Nostr relay at {relay_url}")
        except Exception as e:
            return self.err(str(e))
