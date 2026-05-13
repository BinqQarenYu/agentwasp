from __future__ import annotations
import json
import re as _re

import redis.asyncio as aioredis
import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

TEMPLATE_INDEX_KEY = "output:template:_index"

# ── Known asset prefixes ──────────────────────────────────────────────────────
# Maps lowercase prefix → (TICKER, Full Name)
_ASSET_ALIASES: dict[str, tuple[str, str]] = {
    "btc":   ("BTC",   "Bitcoin"),
    "eth":   ("ETH",   "Ethereum"),
    "sol":   ("SOL",   "Solana"),
    "bnb":   ("BNB",   "Binance Coin"),
    "xrp":   ("XRP",   "Ripple"),
    "ada":   ("ADA",   "Cardano"),
    "doge":  ("DOGE",  "Dogecoin"),
    "matic": ("MATIC", "Polygon"),
    "dot":   ("DOT",   "Polkadot"),
    "link":  ("LINK",  "Chainlink"),
    "avax":  ("AVAX",  "Avalanche"),
    "uni":   ("UNI",   "Uniswap"),
    "ltc":   ("LTC",   "Litecoin"),
    "atom":  ("ATOM",  "Cosmos"),
}

# All subfields required per asset for a valid asset_monitor report
_ASSET_REQUIRED_SUBFIELDS = ["price", "change", "volume", "high", "low"]

# Detects value-level placeholders that should never appear in a rendered report
_VALUE_PLACEHOLDER_RE = _re.compile(
    r"(?:\$[A-Z]\b)"                    # $X — dollar + single uppercase
    r"|(?:\b[A-Z]%)"                    # Y% — uppercase + percent
    r"|\[[^\]]{1,80}\]"                 # [anything in brackets]
    r"|(?:\bN/A\b)"                     # N/A
    r"|(?:\bpor\s+determinar\b)"        # por determinar
    r"|(?:\bplaceholder\b)",            # literal "placeholder"
    _re.IGNORECASE,
)

# Detects unfilled {variable} template placeholders after rendering
_UNFILLED_PLACEHOLDER_RE = _re.compile(r"\{([a-zA-Z_]\w*)\}")


class RenderReportSkill(SkillBase):
    """Render a named output template with dynamic data.

    Actions:
    - render (default): fill template with data, return formatted string
    - register_template: store a new template in Redis
    - list_templates: list available template keys

    For type='asset_monitor': uses a built-in deterministic renderer.
    ALL asset fields (price, change, volume, high, low) must be present for
    every detected asset — partial data causes a hard failure (no fallback).
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="render_report",
            description=(
                "Render a deterministic report from structured data. "
                "MANDATORY for crypto/asset reports: use type='asset_monitor' with flat kwargs "
                "(btc_price, btc_change, btc_volume, btc_high, btc_low, eth_price, ...). "
                "ALL fields for ALL assets must be real values — no placeholders, no missing fields. "
                "If any field is missing, fetch the data first, then call render_report. "
                "Actions: render (default) | register_template | list_templates. "
                "Example: render_report(type='asset_monitor', format='email', "
                "btc_price='69280.50', btc_change='-1.88', btc_volume='28500000000', "
                "btc_high='71200.00', btc_low='68100.00', eth_price='3500.00', ...)"
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="render | register_template | list_templates",
                    required=False,
                    default="render",
                ),
                SkillParam(
                    name="type",
                    param_type=ParamType.STRING,
                    description="Template type key (e.g. 'asset_monitor', 'daily_summary')",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="data",
                    param_type=ParamType.STRING,
                    description="JSON string of variables to inject into the template",
                    required=False,
                    default="{}",
                ),
                SkillParam(
                    name="format",
                    param_type=ParamType.STRING,
                    description="Output format: email | telegram | plain (default: plain)",
                    required=False,
                    default="plain",
                ),
                SkillParam(
                    name="template",
                    param_type=ParamType.STRING,
                    description="Template string for register_template action. Use {varname} for variables.",
                    required=False,
                    default="",
                ),
            ],
            category="productivity",
            timeout_seconds=10.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        action = str(kwargs.get("action", "render")).strip().lower()
        report_type = str(kwargs.get("type", "")).strip()
        fmt = str(kwargs.get("format", "plain")).strip().lower() or "plain"

        if action == "list_templates":
            return await self._list_templates()

        if not report_type:
            return SkillResult(
                skill_name="render_report",
                success=False,
                output="",
                error="'type' parameter is required",
            )

        if action == "register_template":
            return await self._register_template(
                report_type=report_type,
                fmt=fmt,
                template_str=str(kwargs.get("template", "")),
            )

        # Build flat data dict from kwargs (preferred) or JSON string (legacy)
        _reserved = {"action", "type", "format", "data", "template"}
        flat_data = {k: v for k, v in kwargs.items() if k not in _reserved}
        if not flat_data:
            data_str = str(kwargs.get("data", "{}"))
            try:
                flat_data = json.loads(data_str) if data_str.strip() else {}
            except json.JSONDecodeError:
                try:
                    flat_data = json.loads(_repair_json(data_str))
                except Exception as exc:
                    return SkillResult(
                        skill_name="render_report",
                        success=False,
                        output="",
                        error=f"Invalid JSON in 'data': {exc}",
                    )

        # ── Built-in deterministic renderers ─────────────────────────────────
        if report_type == "asset_monitor":
            return self._render_asset_monitor(flat_data, fmt)

        # ── Generic template renderer (Redis-backed) ──────────────────────────
        return await self._render_dict(report_type=report_type, fmt=fmt, data=flat_data)

    # ── Built-in: asset_monitor ───────────────────────────────────────────────

    def _render_asset_monitor(self, data: dict, fmt: str) -> SkillResult:
        """Deterministic renderer for crypto/asset monitor reports.

        Enforces:
        - ALL detected assets must have ALL required subfields
        - No value-level placeholders ($X, Y%, [text]) in any field
        - Identical structure every execution
        """
        # Normalise all values to strings, strip whitespace
        data = {k: str(v).strip() for k, v in data.items()}

        # Detect which assets are present
        assets: list[tuple[str, str, str]] = []  # (prefix, ticker, name)
        for prefix, (ticker, name) in _ASSET_ALIASES.items():
            if f"{prefix}_price" in data:
                assets.append((prefix, ticker, name))

        if not assets:
            return SkillResult(
                skill_name="render_report",
                success=False,
                output="",
                error=(
                    "No asset data found. Provide at least one asset with all required fields: "
                    "btc_price, btc_change, btc_volume, btc_high, btc_low "
                    "(or eth_*, sol_*, etc.)"
                ),
            )

        # Validate completeness — every detected asset must have ALL subfields
        missing_fields: list[str] = []
        for prefix, ticker, _ in assets:
            for subfield in _ASSET_REQUIRED_SUBFIELDS:
                key = f"{prefix}_{subfield}"
                val = data.get(key, "")
                if not val:
                    missing_fields.append(key)

        if missing_fields:
            return SkillResult(
                skill_name="render_report",
                success=False,
                output="",
                error=(
                    f"INCOMPLETE DATA — missing {len(missing_fields)} required field(s): "
                    f"{', '.join(missing_fields[:10])}. "
                    "Fetch missing data before calling render_report. DO NOT proceed with partial data."
                ),
            )

        # Validate no placeholder values in any field
        placeholder_fields: list[str] = []
        for prefix, ticker, _ in assets:
            for subfield in _ASSET_REQUIRED_SUBFIELDS:
                key = f"{prefix}_{subfield}"
                val = data.get(key, "")
                if _VALUE_PLACEHOLDER_RE.search(val):
                    placeholder_fields.append(f"{key}={val!r}")

        if placeholder_fields:
            return SkillResult(
                skill_name="render_report",
                success=False,
                output="",
                error=(
                    f"PLACEHOLDER VALUES DETECTED in {len(placeholder_fields)} field(s): "
                    f"{', '.join(placeholder_fields[:5])}. "
                    "Replace all placeholders with real data before rendering."
                ),
            )

        # Build report
        from datetime import datetime, timezone
        date_str = data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if fmt == "telegram":
            rendered = self._build_telegram_summary(assets, data, date_str)
        else:
            rendered = self._build_email_report(assets, data, date_str)

        return SkillResult(skill_name="render_report", success=True, output=rendered)

    @staticmethod
    def _fmt_volume(raw: str) -> str:
        """Convert raw volume string to compact B/M notation."""
        try:
            v = float(raw.replace(",", "").replace("$", "").strip())
            if v >= 1_000_000_000:
                return f"${v / 1_000_000_000:.1f}B"
            if v >= 1_000_000:
                return f"${v / 1_000_000:.1f}M"
            return f"${raw}"
        except ValueError:
            return f"${raw}"

    @staticmethod
    def _build_email_report(
        assets: list[tuple[str, str, str]],
        data: dict,
        date_str: str,
    ) -> str:

        # ── helpers ────────────────────────────────────────────────────────
        def _ch(prefix: str):
            change = data[f"{prefix}_change"].lstrip("+")
            try:
                ch = float(change)
                return ch, ("+" if ch >= 0 else ""), ("▲" if ch >= 0 else "▼")
            except ValueError:
                return 0.0, "", "→"

        # ── Title ──────────────────────────────────────────────────────────
        tickers_str = " / ".join(ticker for _, ticker, _ in assets)
        lines: list[str] = [
            f"📊 Informe Crypto — {tickers_str}",
            f"{date_str}",
            "",
        ]

        # ── 1. Resumen ejecutivo ────────────────────────────────────────────
        lines += ["1. RESUMEN EJECUTIVO", ""]
        pos_count = neg_count = 0
        # Pre-compute for alignment
        rows = []
        for prefix, ticker, _ in assets:
            price  = data[f"{prefix}_price"]
            change = data[f"{prefix}_change"].lstrip("+")
            ch_val, sign, arrow = _ch(prefix)
            if ch_val >= 0:
                pos_count += 1
            else:
                neg_count += 1
            rows.append((ticker, price, arrow, sign, change))

        # Align price column
        max_price_len = max(len(f"${r[1]}") for r in rows)
        for ticker, price, arrow, sign, change in rows:
            price_col = f"${price}".ljust(max_price_len)
            lines.append(f"{ticker.ljust(4)} → {price_col}   {arrow} {sign}{change}%")

        # Trend summary
        if neg_count == 0:
            trend_label = "📈 Alcista generalizada"
        elif pos_count == 0:
            trend_label = "📉 Bajista generalizada"
        elif neg_count > pos_count:
            trend_label = "📉 Tendencia bajista"
        else:
            trend_label = "📊 Mercado mixto"
        lines += ["", f"{trend_label}", ""]

        # ── 2. Detalle por activo ───────────────────────────────────────────
        lines += ["2. DETALLE POR ACTIVO", ""]
        for prefix, ticker, name in assets:
            price  = data[f"{prefix}_price"]
            volume = data[f"{prefix}_volume"]
            high   = data[f"{prefix}_high"]
            low    = data[f"{prefix}_low"]
            ch_val, sign, arrow = _ch(prefix)
            change = data[f"{prefix}_change"].lstrip("+")
            chg_str = f"{arrow} {sign}{change}%"
            vol_str = RenderReportSkill._fmt_volume(volume)
            lines += [
                f"{ticker} ({name})",
                f"Precio: ${price}",
                f"Cambio 24h: {chg_str}",
                f"Volumen: {vol_str}",
                f"Rango 24h: ${low} — ${high}",
                "",
            ]

        # ── 3. Análisis ─────────────────────────────────────────────────────
        _analysis_fields = [
            ("comparison",     "Comparación"),
            ("trend",          "Tendencia"),
            ("risk",           "Riesgo"),
            ("opportunity",    "Oportunidad"),
            ("interpretation", "Interpretación"),
        ]
        has_analysis = any(data.get(k) for k, _ in _analysis_fields)
        if has_analysis:
            lines += ["3. ANÁLISIS", ""]
            for key, label in _analysis_fields:
                val = data.get(key)
                if val:
                    lines += [label, f"→ {val}", ""]

        return "\n".join(lines).rstrip()

    @staticmethod
    def _build_telegram_summary(
        assets: list[tuple[str, str, str]],
        data: dict,
        date_str: str,
    ) -> str:
        pos_count = neg_count = 0
        rows = []
        for prefix, ticker, _ in assets:
            price  = data[f"{prefix}_price"]
            change = data[f"{prefix}_change"].lstrip("+")
            try:
                ch = float(change)
                sign = "+" if ch >= 0 else ""
                arrow = "▲" if ch >= 0 else "▼"
                if ch >= 0:
                    pos_count += 1
                else:
                    neg_count += 1
            except ValueError:
                sign, arrow = "", "→"
            # Compact price: strip trailing zeros after decimal for readability
            try:
                price_compact = f"{float(price.replace(',', '')):,.0f}"
            except ValueError:
                price_compact = price
            rows.append(f"{ticker} → ${price_compact} {arrow} {sign}{change}%")

        # Trend
        if neg_count == 0:
            trend_str = "📈 Alcista"
        elif pos_count == 0:
            trend_str = "📉 Bajista"
        elif neg_count > pos_count:
            trend_str = "📉 Bajista"
        else:
            trend_str = "📊 Mixto"

        lines = ["📊 Crypto Snapshot", ""] + rows + ["", f"📉 Tendencia: {trend_str.split(' ', 1)[1]}", "", "📧 Informe completo enviado"]
        # Replace trend emoji to match actual direction
        if "Alcista" in trend_str:
            lines[-3] = f"📈 Tendencia: Alcista"
        elif "Mixto" in trend_str:
            lines[-3] = f"📊 Tendencia: Mixta"
        return "\n".join(lines)

    # ── Generic Redis-backed renderer ─────────────────────────────────────────

    async def _render_dict(self, report_type: str, fmt: str, data: dict) -> SkillResult:
        """Render using a Redis-stored template (flat kwargs mode)."""
        template_str = await self._load_template(report_type, fmt)
        if template_str is None:
            lines = [f"[{report_type}]"] + [f"  {k}: {v}" for k, v in data.items()]
            return SkillResult(
                skill_name="render_report",
                success=True,
                output="\n".join(lines),
                error=f"No template for '{report_type}:{fmt}' — fallback used",
            )
        try:
            rendered = _safe_format(template_str, data)
            # Check for unfilled {placeholder} patterns
            unfilled = _check_unfilled_placeholders(rendered)
            if unfilled:
                return SkillResult(
                    skill_name="render_report",
                    success=False,
                    output="",
                    error=(
                        f"Report has {len(unfilled)} unfilled placeholder(s): "
                        f"{', '.join(unfilled[:5])}. Provide missing data before rendering."
                    ),
                )
            # Check for value-level placeholders
            bad = _VALUE_PLACEHOLDER_RE.findall(rendered)
            if bad:
                return SkillResult(
                    skill_name="render_report",
                    success=False,
                    output="",
                    error=(
                        f"Placeholder values detected in rendered output: {bad[:5]}. "
                        "Replace all placeholders with real data."
                    ),
                )
            return SkillResult(skill_name="render_report", success=True, output=rendered)
        except Exception as exc:
            return SkillResult(
                skill_name="render_report", success=False, output="", error=f"Render failed: {exc}"
            )

    async def _register_template(self, report_type: str, fmt: str, template_str: str) -> SkillResult:
        if not template_str:
            return SkillResult(
                skill_name="render_report",
                success=False,
                output="",
                error="'template' parameter is required",
            )
        key = f"output:template:{report_type}:{fmt}"
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.set(key, template_str)
            await r.sadd(TEMPLATE_INDEX_KEY, key)
            return SkillResult(
                skill_name="render_report",
                success=True,
                output=f"Template registered: {key} ({len(template_str)} chars)",
            )
        except Exception as exc:
            return SkillResult(
                skill_name="render_report", success=False, output="", error=str(exc)
            )
        finally:
            await r.aclose()

    async def _list_templates(self) -> SkillResult:
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            members = await r.smembers(TEMPLATE_INDEX_KEY)
            if not members:
                return SkillResult(
                    skill_name="render_report",
                    success=True,
                    output="No templates registered yet. Built-in: asset_monitor (email|telegram).",
                )
            return SkillResult(
                skill_name="render_report",
                success=True,
                output="Registered templates:\n" + "\n".join(sorted(members)),
            )
        except Exception as exc:
            return SkillResult(
                skill_name="render_report", success=False, output="", error=str(exc)
            )
        finally:
            await r.aclose()

    async def _load_template(self, report_type: str, fmt: str) -> str | None:
        keys_to_try = [
            f"output:template:{report_type}:{fmt}",
            f"output:template:{report_type}:default",
            f"output:template:{report_type}:plain",
        ]
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            values = await r.mget(keys_to_try)
            for v in values:
                if v:
                    return v
            return None
        finally:
            await r.aclose()


# ── Utility functions ─────────────────────────────────────────────────────────

def _safe_format(template: str, data: dict) -> str:
    """Format template with {key} substitution. Missing keys stay as literal {key}."""
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"
    return template.format_map(_SafeDict(data))


def _check_unfilled_placeholders(rendered: str) -> list[str]:
    """Return list of unfilled {placeholder} names remaining in rendered output."""
    unfilled = _UNFILLED_PLACEHOLDER_RE.findall(rendered)
    return [u for u in unfilled if u.isidentifier() and len(u) > 1]


def _repair_json(text: str) -> str:
    """Attempt to repair common LLM JSON generation issues."""
    import re as _re
    import ast as _ast

    stripped = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    repaired = _re.sub(r",\s*([}\]])", r"\1", stripped)
    try:
        json.loads(repaired)
        return repaired
    except Exception:
        pass
    try:
        val = _ast.literal_eval(stripped)
        if isinstance(val, dict):
            return json.dumps(val)
    except Exception:
        pass
    return repaired
