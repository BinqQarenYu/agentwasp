"""Grok-style Auto-Review Approval Engine, Plain-English Rules & Prompt-Injection Defense.

Implements the full approval model across Agent WASP (both native skills and external
CLI harnesses via `waspctl approve`):
- Plain-English `ask_first` and `allow_auto` rules (up to 20 per kind, 1,000 chars each).
- Precedence invariants:
    1. External untrusted content (emails, web pages, files) NEVER auto-executes
       risky actions — prompt-injection defense overrides `allow_auto`.
    2. `ask_first` ALWAYS beats `allow_auto` when both match.
    3. `team` rules set a mandatory floor; `personal` rules can only tighten policy.
    4. Built-in 7-category safety floor (messaging, publishing, financial, destructive,
       permissions, production, legal) requires approval unless explicitly auto-allowed.
- Interactive Approval Cards (`allow_once`, `always_allow`, `deny`) with 30-minute
  default expiry for background routines and immediate closing on new user messages.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import structlog

logger = structlog.get_logger()

MAX_RULES_PER_KIND = 20
MAX_RULE_CHARS = 1000
ROUTINE_CARD_TTL_SECONDS = 1800.0  # 30 minutes for background routines


class RuleKind(str, Enum):
    ASK_FIRST = "ask_first"
    ALLOW_AUTO = "allow_auto"


class RuleScope(str, Enum):
    TEAM = "team"
    PERSONAL = "personal"


class ActionOrigin(str, Enum):
    USER_DIRECT = "user_direct"
    ROUTINE = "routine"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    PEER_BOT = "peer_bot"


class RiskCategory(str, Enum):
    NONE = "none"
    MESSAGING = "messaging"
    PUBLISHING = "publishing"
    FINANCIAL = "financial"
    DESTRUCTIVE = "destructive"
    PERMISSIONS = "permissions"
    PRODUCTION = "production"
    LEGAL = "legal"


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENY = "deny"


class CardStatus(str, Enum):
    PENDING = "pending"
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENIED = "denied"
    EXPIRED = "expired"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# Built-in 7-category safety patterns (irreversible / high-impact actions)
# ---------------------------------------------------------------------------

_RISK_PATTERNS: list[tuple[RiskCategory, re.Pattern[str], str]] = [
    (
        RiskCategory.FINANCIAL,
        re.compile(
            r"\b(?:purchase|buy|checkout|pay|payment|wire\s+transfer|transfer\s+money|"
            r"stripe\.charge|billing\.pay|invoice\.pay|refund|crypto\.send|place\s+order|"
            r"send\s+money|debit|credit\s+card)\b",
            re.IGNORECASE,
        ),
        "Financial action — charges or transfers may not be reversible.",
    ),
    (
        RiskCategory.DESTRUCTIVE,
        re.compile(
            r"(?:\brm\s+-rf\b|\bdel\s+/[fsq]|\bdrop\s+(?:table|database)\b|\btruncate\s+table\b|"
            r"\bgit\s+reset\s+--hard\b|\bgit\s+push\s+(?:--force|-f)\b|"
            r"\b(?:delete|wipe|erase|purge|destroy|overwrite)\b\s+(?:all|file|files|folder|repo|database|data|records|account))",
            re.IGNORECASE,
        ),
        "Destructive action — this cannot be undone.",
    ),
    (
        RiskCategory.PERMISSIONS,
        re.compile(
            r"\b(?:chmod|chown|sudo|grant\s+admin|revoke\s+access|iam\.|change\s+permissions?|"
            r"add\s+ssh\s+key|rotate\s+secret|modify\s+role|set\s+acl|icacls)\b",
            re.IGNORECASE,
        ),
        "Security & permission change — alters access control boundaries.",
    ),
    (
        RiskCategory.PRODUCTION,
        re.compile(
            r"\b(?:deploy\s+(?:to\s+)?prod(?:uction)?|prod(?:uction)?\s+deploy|db\s+migrate|"
            r"kubectl\s+(?:apply|delete|rollout)|terraform\s+(?:apply|destroy)|"
            r"docker\s+(?:rm|system\s+prune)|restart\s+prod(?:uction)?)\b",
            re.IGNORECASE,
        ),
        "Production change — may impact live services or users.",
    ),
    (
        RiskCategory.LEGAL,
        re.compile(
            r"\b(?:accept\s+(?:terms|tos|eula|agreement|policy)|sign\s+(?:contract|nda|agreement|document)|"
            r"docusign|legally\s+binding)\b",
            re.IGNORECASE,
        ),
        "Legal commitment — accepting terms or signing agreements requires explicit approval.",
    ),
    (
        RiskCategory.PUBLISHING,
        re.compile(
            r"\b(?:publish|post\s+publicly|tweet|social\.post|npm\s+publish|twine\s+upload|"
            r"gh\s+release\s+create|create\s+public\s+repo|make\s+repo\s+public)\b",
            re.IGNORECASE,
        ),
        "Public publishing action — content will be visible externally.",
    ),
    (
        RiskCategory.MESSAGING,
        re.compile(
            r"\b(?:gmail\.send|send[_\s]+email|send[_\s]+message|send[_\s]+invite|"
            r"calendar\.invite|slack\.post|whatsapp\.send|telegram\.send|sms\.send|email\.send)\b",
            re.IGNORECASE,
        ),
        "External communication — sends a message or invite to another person.",
    ),
]

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "when", "before", "after",
        "always", "never", "ask", "first", "allow", "automatically", "auto", "any",
        "running", "run", "call", "calling", "action", "tool", "bot", "user", "into",
    }
)


@dataclass
class ApprovalRule:
    id: str
    kind: RuleKind
    text: str
    scope: RuleScope = RuleScope.PERSONAL
    action_pattern: str = ""
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "text": self.text,
            "action_pattern": self.action_pattern,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRule":
        return cls(
            id=str(data["id"]),
            kind=RuleKind(data["kind"]),
            scope=RuleScope(data.get("scope", "personal")),
            text=str(data["text"]),
            action_pattern=str(data.get("action_pattern") or ""),
            enabled=bool(data.get("enabled", True)),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )


@dataclass(slots=True)
class ReviewResult:
    outcome: str  # "allow" | "ask" | "deny"
    reason: str
    risk_category: RiskCategory = RiskCategory.NONE
    warning: str = ""
    matched_rule_id: str | None = None
    matched_rule_scope: RuleScope | None = None

    @property
    def requires_card(self) -> bool:
        return self.outcome == "ask"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "risk_category": self.risk_category.value,
            "warning": self.warning,
            "matched_rule_id": self.matched_rule_id,
            "matched_rule_scope": self.matched_rule_scope.value if self.matched_rule_scope else None,
            "requires_card": self.requires_card,
        }


@dataclass
class ApprovalCard:
    id: str
    bot_id: str
    bot_name: str
    action_name: str
    action_summary: str
    action_detail: str
    origin: ActionOrigin
    risk_category: RiskCategory
    warning: str
    reason: str
    created_at: str
    expires_at: str | None = None
    requester_id: str = ""
    status: CardStatus = CardStatus.PENDING
    resolved_by: str = ""
    resolution_note: str = ""
    saved_rule_id: str | None = None
    future: asyncio.Future[ApprovalDecision] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "action_name": self.action_name,
            "action_summary": self.action_summary,
            "action_detail": self.action_detail,
            "origin": self.origin.value,
            "risk_category": self.risk_category.value,
            "warning": self.warning,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "requester_id": self.requester_id,
            "status": self.status.value,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
            "saved_rule_id": self.saved_rule_id,
        }


# ---------------------------------------------------------------------------
# Pure deterministic rule matching & evaluation (zero I/O)
# ---------------------------------------------------------------------------


def classify_risk(action_name: str, action_summary: str = "", action_detail: str = "") -> tuple[RiskCategory, str]:
    """Classify an action against the 7 built-in high-risk safety gates."""
    combined = f"{action_name} {action_summary} {action_detail}".strip()
    for category, pattern, warning in _RISK_PATTERNS:
        if pattern.search(combined):
            return category, warning
    return RiskCategory.NONE, ""


def _extract_tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9_./:-]{3,}", text.lower())
        if tok not in _STOPWORDS
    }


def rule_matches_action(
    rule: ApprovalRule,
    action_name: str,
    action_summary: str = "",
    action_detail: str = "",
) -> bool:
    """Match either an explicit `action_pattern` or plain-English rule semantics."""
    if not rule.enabled:
        return False

    combined = f"{action_name} {action_summary} {action_detail}".lower()

    # 1. Explicit action pattern (exact, prefix wildcard `gmail.*`, or substring)
    pat = rule.action_pattern.strip().lower()
    if pat:
        if pat.endswith("*"):
            prefix = pat[:-1]
            if action_name.lower().startswith(prefix) or prefix in combined:
                return True
        elif pat == action_name.lower() or pat in combined:
            return True

    # 2. Quoted literal phrases in plain-English rule (e.g. `running "git status" in "/workspace"`)
    quoted = [q.strip().lower() for q in re.findall(r"[`\"']([^`\"']{2,})[`\"']", rule.text)]
    if quoted:
        return all(q in combined for q in quoted)

    # 3. Plain-English token overlap (requires strong semantic alignment)
    rule_tokens = _extract_tokens(rule.text)
    if not rule_tokens:
        return False
    action_tokens = _extract_tokens(combined)
    overlap = rule_tokens & action_tokens
    if not overlap:
        return False

    # Require at least 2 matching significant tokens, or 1 if rule has only 1-2 tokens
    needed = 1 if len(rule_tokens) <= 2 else 2
    return len(overlap) >= needed


def evaluate_action(
    action_name: str,
    action_summary: str = "",
    action_detail: str = "",
    *,
    rules: list[ApprovalRule] | None = None,
    origin: ActionOrigin = ActionOrigin.USER_DIRECT,
    auto_review_enabled: bool = True,
) -> ReviewResult:
    """Pure policy evaluator enforcing Grok Bot approval precedence invariants."""
    all_rules = [r for r in (rules or []) if r.enabled]
    risk_category, warning = classify_risk(action_name, action_summary, action_detail)

    # 1. Team `ask_first` rules ALWAYS apply — even if auto-review is toggled off.
    for rule in all_rules:
        if rule.scope == RuleScope.TEAM and rule.kind == RuleKind.ASK_FIRST:
            if rule_matches_action(rule, action_name, action_summary, action_detail):
                return ReviewResult(
                    outcome="ask",
                    reason=f"Blocked by mandatory team rule: {rule.text}",
                    risk_category=risk_category,
                    warning=warning or "Mandatory team approval required.",
                    matched_rule_id=rule.id,
                    matched_rule_scope=RuleScope.TEAM,
                )

    # 2. Prompt-injection defense: untrusted external text (emails, web pages, files)
    #    can NEVER auto-execute risky actions, regardless of personal allow rules.
    if origin == ActionOrigin.EXTERNAL_UNTRUSTED and risk_category != RiskCategory.NONE:
        return ReviewResult(
            outcome="ask",
            reason=(
                f"Prompt-injection guard: action triggered by untrusted external content "
                f"classified as '{risk_category.value}'."
            ),
            risk_category=risk_category,
            warning=warning or "Triggered by external content — verify this was your intent.",
        )

    if not auto_review_enabled:
        return ReviewResult(
            outcome="allow",
            reason="Auto-review disabled by operator (no mandatory team ask-first matched).",
            risk_category=risk_category,
        )

    # 3. Personal `ask_first` rules ALWAYS win over `allow_auto`.
    for rule in all_rules:
        if rule.scope == RuleScope.PERSONAL and rule.kind == RuleKind.ASK_FIRST:
            if rule_matches_action(rule, action_name, action_summary, action_detail):
                return ReviewResult(
                    outcome="ask",
                    reason=f"Matched ask-first rule: {rule.text}",
                    risk_category=risk_category,
                    warning=warning,
                    matched_rule_id=rule.id,
                    matched_rule_scope=RuleScope.PERSONAL,
                )

    # 4. Check `allow_auto` rules (team or personal).
    for rule in all_rules:
        if rule.kind == RuleKind.ALLOW_AUTO:
            if rule_matches_action(rule, action_name, action_summary, action_detail):
                return ReviewResult(
                    outcome="allow",
                    reason=f"Auto-allowed by rule: {rule.text}",
                    risk_category=risk_category,
                    matched_rule_id=rule.id,
                    matched_rule_scope=rule.scope,
                )

    # 5. Built-in 7-category safety floor defaults to ASK.
    if risk_category != RiskCategory.NONE:
        return ReviewResult(
            outcome="ask",
            reason=f"Built-in safety check flagged '{risk_category.value}' action.",
            risk_category=risk_category,
            warning=warning,
        )

    # 6. Safe / read-only action runs automatically.
    return ReviewResult(
        outcome="allow",
        reason="Low-risk action cleared by auto-review.",
        risk_category=RiskCategory.NONE,
    )


# ---------------------------------------------------------------------------
# Stateful ApprovalManager (rules persistence + blocking approval cards)
# ---------------------------------------------------------------------------

BroadcastFn = Callable[[dict[str, Any]], None]
ModelReviewerFn = Callable[[str, str, str, ActionOrigin], Awaitable[ReviewResult | None]]


class ApprovalManager:
    """Manages plain-English approval rules and interactive approval cards."""

    def __init__(
        self,
        rules_path: Path | None = None,
        broadcast: BroadcastFn | None = None,
        auto_review_enabled: bool = True,
        model_reviewer: ModelReviewerFn | None = None,
    ) -> None:
        self.rules_path = Path(rules_path) if rules_path else None
        self._broadcast = broadcast
        self.auto_review_enabled = auto_review_enabled
        self._model_reviewer = model_reviewer
        self._rules: dict[str, ApprovalRule] = {}
        self._cards: dict[str, ApprovalCard] = {}
        self._load_rules()

    def set_broadcaster(self, fn: BroadcastFn) -> None:
        self._broadcast = fn

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._broadcast is not None:
            try:
                self._broadcast(payload)
            except Exception:
                pass

    def _load_rules(self) -> None:
        if not self.rules_path or not self.rules_path.exists():
            return
        try:
            raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    rule = ApprovalRule.from_dict(item)
                    self._rules[rule.id] = rule
        except Exception as exc:
            logger.warning("approvals.load_rules_failed", error=str(exc)[:100])

    def _save_rules(self) -> None:
        if not self.rules_path:
            return
        try:
            self.rules_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.rules_path.with_suffix(".tmp")
            data = [r.to_dict() for r in self._rules.values()]
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.rules_path)
        except Exception as exc:
            logger.warning("approvals.save_rules_failed", error=str(exc)[:100])

    # ------------------------------------------------------------------
    # Rule CRUD (enforces Grok Bot limits: <= 20 per kind, <= 1000 chars)
    # ------------------------------------------------------------------

    def list_rules(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._rules.values()]

    def add_rule(
        self,
        *,
        kind: RuleKind | str,
        text: str,
        scope: RuleScope | str = RuleScope.PERSONAL,
        action_pattern: str = "",
    ) -> ApprovalRule:
        rule_kind = RuleKind(kind)
        rule_scope = RuleScope(scope)
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Rule text cannot be empty.")
        if len(cleaned) > MAX_RULE_CHARS:
            raise ValueError(f"Rule text exceeds maximum length of {MAX_RULE_CHARS} characters.")

        same_bucket = [
            r for r in self._rules.values() if r.kind == rule_kind and r.scope == rule_scope
        ]
        if len(same_bucket) >= MAX_RULES_PER_KIND:
            raise ValueError(
                f"Maximum of {MAX_RULES_PER_KIND} '{rule_kind.value}' rules reached for '{rule_scope.value}' scope."
            )

        rule = ApprovalRule(
            id=f"apr_{uuid4().hex[:10]}",
            kind=rule_kind,
            scope=rule_scope,
            text=cleaned,
            action_pattern=(action_pattern or "").strip(),
        )
        self._rules[rule.id] = rule
        self._save_rules()
        self._emit({"type": "approval_rule_added", "rule": rule.to_dict()})
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        existed = self._rules.pop(rule_id, None) is not None
        if existed:
            self._save_rules()
            self._emit({"type": "approval_rule_deleted", "rule_id": rule_id})
        return existed

    # ------------------------------------------------------------------
    # Review & Blocking Card Execution
    # ------------------------------------------------------------------

    async def review_action(
        self,
        action_name: str,
        action_summary: str = "",
        action_detail: str = "",
        *,
        origin: ActionOrigin = ActionOrigin.USER_DIRECT,
    ) -> ReviewResult:
        result = evaluate_action(
            action_name,
            action_summary,
            action_detail,
            rules=list(self._rules.values()),
            origin=origin,
            auto_review_enabled=self.auto_review_enabled,
        )
        # If deterministic policy said "allow" without an explicit allow_auto rule,
        # optionally let a model reviewer catch nuanced risks.
        if (
            result.outcome == "allow"
            and result.matched_rule_id is None
            and self.auto_review_enabled
            and self._model_reviewer is not None
        ):
            try:
                model_res = await self._model_reviewer(action_name, action_summary, action_detail, origin)
                if model_res is not None and model_res.outcome in ("ask", "deny"):
                    return model_res
            except Exception as exc:
                logger.warning("approvals.model_reviewer_error", error=str(exc)[:100])
        return result

    async def request_approval(
        self,
        *,
        bot_id: str,
        bot_name: str,
        action_name: str,
        action_summary: str,
        action_detail: str = "",
        origin: ActionOrigin | str = ActionOrigin.USER_DIRECT,
        requester_id: str = "",
        timeout_seconds: float | None = None,
    ) -> tuple[bool, ReviewResult, ApprovalCard | None]:
        """Gate an action: returns `(approved, review_result, card_or_none)`."""
        origin_enum = ActionOrigin(origin) if isinstance(origin, str) else origin
        review = await self.review_action(
            action_name,
            action_summary,
            action_detail,
            origin=origin_enum,
        )
        if review.outcome == "allow":
            return True, review, None
        if review.outcome == "deny":
            return False, review, None

        # Determine expiry: background routines expire in 30m by default; chats wait unless timeout given.
        effective_timeout = timeout_seconds
        if effective_timeout is None and origin_enum == ActionOrigin.ROUTINE:
            effective_timeout = ROUTINE_CARD_TTL_SECONDS

        now = datetime.now(timezone.utc)
        expires_at = (
            (now + timedelta(seconds=effective_timeout)).isoformat()
            if effective_timeout is not None
            else None
        )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        card = ApprovalCard(
            id=f"card_{uuid4().hex[:10]}",
            bot_id=bot_id,
            bot_name=bot_name or bot_id,
            action_name=action_name,
            action_summary=action_summary,
            action_detail=action_detail,
            origin=origin_enum,
            risk_category=review.risk_category,
            warning=review.warning,
            reason=review.reason,
            created_at=now.isoformat(),
            expires_at=expires_at,
            requester_id=requester_id,
            future=fut,
        )
        self._cards[card.id] = card
        self._emit({"type": "approval_requested", "card": card.to_dict()})

        try:
            decision = (
                await asyncio.wait_for(fut, timeout=effective_timeout)
                if effective_timeout is not None
                else await fut
            )
            approved = decision in (ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALWAYS_ALLOW)
            return approved, review, card
        except asyncio.TimeoutError:
            card.status = CardStatus.EXPIRED
            self._emit({"type": "approval_expired", "card": card.to_dict()})
            return False, review, card
        except asyncio.CancelledError:
            if card.status == CardStatus.PENDING:
                card.status = CardStatus.CLOSED
                self._emit({"type": "approval_closed", "card": card.to_dict()})
            return False, review, card

    def decide_card(
        self,
        card_id: str,
        decision: ApprovalDecision | str,
        *,
        actor_id: str = "",
        note: str = "",
    ) -> ApprovalCard:
        card = self._cards.get(card_id)
        if not card or card.status != CardStatus.PENDING:
            raise KeyError(f"Pending approval card '{card_id}' not found.")

        # In group chats, only the person who asked can approve.
        if card.requester_id and actor_id and actor_id != card.requester_id:
            raise PermissionError("Only the requester who initiated this action can approve it.")

        dec = ApprovalDecision(decision) if isinstance(decision, str) else decision
        card.resolved_by = actor_id or "operator"
        card.resolution_note = note

        if dec == ApprovalDecision.DENY:
            card.status = CardStatus.DENIED
        elif dec == ApprovalDecision.ALLOW_ONCE:
            card.status = CardStatus.ALLOW_ONCE
        elif dec == ApprovalDecision.ALWAYS_ALLOW:
            # Never let a personal Always-Allow bypass a mandatory team Ask-First rule.
            team_blocked = any(
                r.scope == RuleScope.TEAM
                and r.kind == RuleKind.ASK_FIRST
                and rule_matches_action(r, card.action_name, card.action_summary, card.action_detail)
                for r in self._rules.values()
            )
            if team_blocked:
                card.status = CardStatus.ALLOW_ONCE
                card.resolution_note = (
                    f"{note} (Allowed once; mandatory team ask-first rule prevents Always Allow.)"
                ).strip()
            else:
                card.status = CardStatus.ALWAYS_ALLOW
                try:
                    saved = self.add_rule(
                        kind=RuleKind.ALLOW_AUTO,
                        scope=RuleScope.PERSONAL,
                        text=f'Allow automatically when running "{card.action_name}"',
                        action_pattern=card.action_name,
                    )
                    card.saved_rule_id = saved.id
                except ValueError as exc:
                    card.resolution_note = f"{note} (Rule limit reached: {exc})".strip()

        if card.future and not card.future.done():
            card.future.set_result(dec)
        self._emit({"type": "approval_resolved", "card": card.to_dict()})
        return card

    def close_open_cards_for_bot(self, bot_id: str, reason: str = "New user message received") -> int:
        """Close pending approval cards for a bot when the user sends a new message."""
        closed = 0
        for card in self._cards.values():
            if card.bot_id == bot_id and card.status == CardStatus.PENDING:
                card.status = CardStatus.CLOSED
                card.resolution_note = reason
                if card.future and not card.future.done():
                    card.future.set_result(ApprovalDecision.DENY)
                self._emit({"type": "approval_closed", "card": card.to_dict()})
                closed += 1
        return closed

    def list_cards(self, pending_only: bool = False) -> list[dict[str, Any]]:
        cards = list(self._cards.values())
        if pending_only:
            cards = [c for c in cards if c.status == CardStatus.PENDING]
        return [c.to_dict() for c in cards]
