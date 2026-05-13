"""Integration platform tests — manifest validation, policy gating, vault security.

These tests are pure unit tests: no external API calls required.
Run with: pytest tests/test_integrations_platform.py -v

Environment:
    DATABASE_URL="postgresql+asyncpg://x:x@x/x"  (required by Settings)
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path
from typing import Any

import pytest

# ── Ensure src is on path ───────────────────────────────────────────────────
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Lazy import helpers ─────────────────────────────────────────────────────

def _load_connector_classes() -> list[type]:
    """Import all connector modules and collect BaseConnector subclasses."""
    from integrations.base import BaseConnector

    connectors_pkg_path = _SRC / "integrations" / "connectors"
    classes: list[type] = []

    for mod_info in pkgutil.iter_modules([str(connectors_pkg_path)]):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"integrations.connectors.{mod_info.name}")
        except ImportError as e:
            # Skip connectors with optional dependencies not installed
            pytest.skip(f"Skipping {mod_info.name}: {e}")
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseConnector)
                and attr is not BaseConnector
                and not attr.__name__.startswith("_")
                and not getattr(attr, "__abstractmethods__", None)
            ):
                # Skip platform_base since it's abstract-ish
                if attr.__module__.endswith("platform_base"):
                    continue
                classes.append(attr)

    return classes


@pytest.fixture(scope="module")
def all_connector_classes():
    return _load_connector_classes()


@pytest.fixture(scope="module")
def all_manifests(all_connector_classes):
    manifests = []
    for cls in all_connector_classes:
        try:
            instance = cls()
            manifests.append((cls.__name__, instance.manifest()))
        except Exception as e:
            pytest.fail(f"Failed to instantiate {cls.__name__}: {e}")
    return manifests


# ────────────────────────────────────────────────────────────────────────────
# 1. Manifest completeness
# ────────────────────────────────────────────────────────────────────────────

class TestManifestCompleteness:
    def test_has_id(self, all_manifests):
        for name, m in all_manifests:
            assert m.id, f"{name}: manifest.id must not be empty"

    def test_id_is_kebab_or_snake_case(self, all_manifests):
        # Updated: the codebase uses kebab-case for some connectors (philips-hue)
        # and snake_case for others (google_calendar). Both are referenced as IDs
        # across templates/routes/OAuth flows. Allow both rather than break
        # user-visible URLs and existing token storage keys.
        import re
        for name, m in all_manifests:
            assert re.match(r'^[a-z0-9]+([-_][a-z0-9]+)*$', m.id), \
                f"{name}: id '{m.id}' must be kebab-case or snake_case"

    def test_has_version(self, all_manifests):
        for name, m in all_manifests:
            assert m.version, f"{name}: manifest.version must not be empty"

    def test_has_name(self, all_manifests):
        for name, m in all_manifests:
            assert m.name, f"{name}: manifest.name must not be empty"

    def test_has_description(self, all_manifests):
        for name, m in all_manifests:
            assert m.description, f"{name}: manifest.description must not be empty"

    def test_has_actions(self, all_manifests):
        for name, m in all_manifests:
            assert len(m.actions) > 0, f"{name}: manifest.actions must not be empty"

    def test_has_required_secrets_list(self, all_manifests):
        for name, m in all_manifests:
            assert isinstance(m.required_secrets, list), \
                f"{name}: required_secrets must be a list"

    def test_has_rate_limits(self, all_manifests):
        for name, m in all_manifests:
            assert isinstance(m.rate_limits, dict), \
                f"{name}: rate_limits must be a dict"


# ────────────────────────────────────────────────────────────────────────────
# 2. Rate limit coverage — every action should have a rate_limit entry
# ────────────────────────────────────────────────────────────────────────────

class TestRateLimitCoverage:
    def test_all_actions_have_rate_limits(self, all_manifests):
        missing = []
        for name, m in all_manifests:
            for action in m.actions:
                if action.id not in m.rate_limits:
                    missing.append(f"{name}.{action.id}")
        assert not missing, f"Actions missing rate limits: {missing}"


# ────────────────────────────────────────────────────────────────────────────
# 3. Risk coherence — manifest.risk_level == max(action.risk_levels)
# ────────────────────────────────────────────────────────────────────────────

class TestRiskCoherence:
    _ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    def test_manifest_risk_matches_max_action_risk(self, all_manifests):
        mismatches = []
        for name, m in all_manifests:
            if not m.actions:
                continue
            max_action_risk = max(
                self._ORDER.get(a.risk_level.value, 0) for a in m.actions
            )
            manifest_risk = self._ORDER.get(m.risk_level.value, 0)
            if manifest_risk < max_action_risk:
                mismatches.append(
                    f"{name}: manifest risk={m.risk_level.value} "
                    f"but max action risk is higher"
                )
        assert not mismatches, f"Risk level mismatches: {mismatches}"


# ────────────────────────────────────────────────────────────────────────────
# 4. CRITICAL actions always blocked by PolicyEngine
# ────────────────────────────────────────────────────────────────────────────

class TestPolicyGating:
    @pytest.mark.asyncio
    async def test_critical_risk_always_blocked(self):
        from integrations.policy import PolicyEngine
        from integrations.base import RiskLevel

        policy = PolicyEngine(redis_url="redis://localhost:0")
        result = await policy.gate(
            integration_id="test-integration",
            action_id="test-action",
            risk_level=RiskLevel.CRITICAL,
        )
        assert not result.allowed, "CRITICAL risk actions must always be blocked"

    @pytest.mark.asyncio
    async def test_low_risk_allowed_by_default(self):
        from integrations.policy import PolicyEngine
        from integrations.base import RiskLevel

        policy = PolicyEngine(redis_url="redis://localhost:0")
        policy._enabled.add("test-integration")  # Enable integration for test
        result = await policy.gate(
            integration_id="test-integration",
            action_id="test-action",
            risk_level=RiskLevel.LOW,
        )
        assert result.allowed, "LOW risk actions should be allowed for an enabled integration"

    @pytest.mark.asyncio
    async def test_disabled_integration_blocked(self):
        from integrations.policy import PolicyEngine
        from integrations.base import RiskLevel

        policy = PolicyEngine(redis_url="redis://localhost:0")
        policy._enabled.add("disabled-integration")  # Enable first
        await policy.disable("disabled-integration")  # Then disable (Redis write fails silently)
        result = await policy.gate(
            integration_id="disabled-integration",
            action_id="any-action",
            risk_level=RiskLevel.LOW,
        )
        assert not result.allowed, "Disabled integrations must be blocked even for LOW risk"


# ────────────────────────────────────────────────────────────────────────────
# 5. Capability values — each ActionSpec.capability is in allowed set
# ────────────────────────────────────────────────────────────────────────────

_VALID_CAPABILITIES = {"monitored", "controlled", "restricted"}


class TestCapabilityValues:
    def test_action_capability_is_valid(self, all_manifests):
        invalid = []
        for name, m in all_manifests:
            for action in m.actions:
                if action.capability not in _VALID_CAPABILITIES:
                    invalid.append(
                        f"{name}.{action.id}: capability='{action.capability}'"
                        f" not in {_VALID_CAPABILITIES}"
                    )
        assert not invalid, f"Invalid capability values: {invalid}"


# ────────────────────────────────────────────────────────────────────────────
# 6. 1Password non-leak — get_item_metadata returns no field values
# ────────────────────────────────────────────────────────────────────────────

class TestOnePasswordNonLeak:
    def test_no_secret_keys_in_response(self):
        """Ensure 1Password connector strips secret field values from response."""
        from integrations.connectors.onepassword import OnePasswordConnector

        conn = OnePasswordConnector()
        # Simulate a raw API response with secret fields
        raw_item = {
            "id": "abc123",
            "title": "My Login",
            "category": "LOGIN",
            "urls": [{"href": "https://example.com"}],
            "updated_at": "2024-01-01T00:00:00Z",
            "fields": [
                {"id": "username", "label": "username", "value": "admin"},
                {"id": "password", "label": "password", "value": "super-secret"},
                {"id": "token", "label": "token", "value": "tok_abc123"},
            ],
        }
        # Call the internal redaction helper
        redacted = conn._redact_secret_fields(raw_item)

        # Fields array should be removed or all values redacted
        fields = redacted.get("fields", [])
        for field in fields:
            assert field.get("value") != "super-secret", \
                "Password value must be redacted"
            assert field.get("value") != "tok_abc123", \
                "Token value must be redacted"

        # Top-level keys named value/password/secret/credential/token must be redacted
        for key in ("value", "password", "secret", "credential", "token"):
            val = redacted.get(key)
            if val is not None:
                assert val == "[REDACTED]", f"Key '{key}' must be redacted, got: {val!r}"

    def test_get_item_reference_returns_uri_string(self):
        """get_item_reference must return an op:// URI, never call the API."""
        from integrations.connectors.onepassword import OnePasswordConnector
        import asyncio

        conn = OnePasswordConnector()

        async def run():
            return await conn.execute(
                "get_item_reference",
                params={"vault_name": "Personal", "item_title": "GitHub", "field_label": "password"},
                secrets={"connect_host": "dummy"},
            )

        # asyncio.get_event_loop() is deprecated in Python 3.12 — use asyncio.run.
        result = asyncio.run(run())
        assert result["ok"], f"get_item_reference failed: {result}"
        ref = result["data"]
        assert isinstance(ref, str), "get_item_reference must return a string"
        assert ref.startswith("op://"), f"Reference must start with op://, got: {ref}"
        # Ensure no actual secret value leaks
        assert "password" not in ref.lower() or ref == "op://Personal/GitHub/password", \
            f"Reference contains unexpected content: {ref}"


# ────────────────────────────────────────────────────────────────────────────
# 7. Platform bridge allowlist — no arbitrary command execution
# ────────────────────────────────────────────────────────────────────────────

_FORBIDDEN_ACTION_PATTERNS = ["exec", "run_command", "shell", "eval", "arbitrary"]


class TestPlatformBridgeAllowlist:
    def test_no_arbitrary_exec_actions(self, all_manifests):
        violations = []
        for name, m in all_manifests:
            if "platform" not in m.category:
                continue
            for action in m.actions:
                for pattern in _FORBIDDEN_ACTION_PATTERNS:
                    if pattern in action.id.lower():
                        violations.append(f"{name}.{action.id} contains '{pattern}'")
        assert not violations, \
            f"Platform bridge connectors must not expose arbitrary exec: {violations}"


# ────────────────────────────────────────────────────────────────────────────
# 8. Required secrets appear in manifest
# ────────────────────────────────────────────────────────────────────────────

class TestRequiredSecretsInManifest:
    def test_required_secrets_are_strings(self, all_manifests):
        invalid = []
        for name, m in all_manifests:
            for s in m.required_secrets:
                if not isinstance(s, str) or not s:
                    invalid.append(f"{name}: secret entry {s!r} must be non-empty string")
        assert not invalid, f"Invalid required_secrets entries: {invalid}"

    def test_required_secrets_no_duplicates(self, all_manifests):
        dupes = []
        for name, m in all_manifests:
            seen = set()
            for s in m.required_secrets:
                if s in seen:
                    dupes.append(f"{name}: duplicate secret '{s}'")
                seen.add(s)
        assert not dupes, f"Duplicate secrets: {dupes}"


# ────────────────────────────────────────────────────────────────────────────
# 9. Unique connector IDs — no two registered connectors share an id
# ────────────────────────────────────────────────────────────────────────────

class TestUniqueConnectorIds:
    def test_all_ids_unique(self, all_manifests):
        seen_ids: dict[str, str] = {}
        dupes = []
        for cls_name, m in all_manifests:
            if m.id in seen_ids:
                dupes.append(f"ID '{m.id}' used by both {seen_ids[m.id]} and {cls_name}")
            else:
                seen_ids[m.id] = cls_name
        assert not dupes, f"Duplicate connector IDs: {dupes}"


# ────────────────────────────────────────────────────────────────────────────
# 10. Action IDs are unique within each connector
# ────────────────────────────────────────────────────────────────────────────

class TestActionIdUniqueness:
    def test_action_ids_unique_per_connector(self, all_manifests):
        dupes = []
        for name, m in all_manifests:
            seen = set()
            for action in m.actions:
                if action.id in seen:
                    dupes.append(f"{name}: duplicate action id '{action.id}'")
                seen.add(action.id)
        assert not dupes, f"Duplicate action IDs within connectors: {dupes}"


# ────────────────────────────────────────────────────────────────────────────
# 11. Capability catalog validation
# ────────────────────────────────────────────────────────────────────────────

class TestCapabilityCatalog:
    def test_catalog_has_canonical_names(self):
        from integrations.capability_catalog import CAPABILITIES
        assert isinstance(CAPABILITIES, dict)
        assert len(CAPABILITIES) >= 20, "Capability catalog should have at least 20 entries"
        for key, desc in CAPABILITIES.items():
            assert isinstance(key, str) and key, "Catalog key must be non-empty string"
            assert isinstance(desc, str) and desc, "Catalog description must be non-empty string"
            assert " " not in key, f"Catalog key '{key}' must be snake_case (no spaces)"

    def test_catalog_keys_are_snake_case(self):
        from integrations.capability_catalog import CAPABILITIES
        import re
        for key in CAPABILITIES:
            assert re.match(r'^[a-z][a-z0-9_]*$', key), \
                f"Catalog key '{key}' must be snake_case"


# ────────────────────────────────────────────────────────────────────────────
# 12. New Phase 3 connector IDs are registered
# ────────────────────────────────────────────────────────────────────────────

class TestPhase3ConnectorPresence:
    _EXPECTED_IDS = [
        "webchat",
        "1password",
        "whatsapp-baileys",
        "teams",
        "bluebubbles",
        "nostr",
        "zalo",
        "platform-macos",
        "platform-ios",
        "platform-android",
        "platform-windows",
        "platform-linux",
    ]

    def test_expected_connector_ids_present(self, all_manifests):
        manifest_ids = {m.id for _, m in all_manifests}
        missing = [eid for eid in self._EXPECTED_IDS if eid not in manifest_ids]
        assert not missing, f"Missing expected Phase 3 connector IDs: {missing}"
