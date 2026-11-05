"""Deterministic business-scoped owner-rule evaluation before model access."""

from collections.abc import Sequence

from app.domain.types import Action, Intent, OwnerRule, PolicyResult


class PolicyEngine:
    """Pure deterministic authority for owner-defined response capabilities. Filter rules by
    business, intent, and enablement, then use priority and ID for stable selection. It owns
    no mutable state/resources; the response model cannot introduce capabilities or
    interpret arbitrary rule text.
    """

    version = "owner_policy:v1"

    def evaluate(
        self, business_id: str, intent: Intent, rules: Sequence[OwnerRule]
    ) -> PolicyResult:
        """Return the highest-priority enabled matching rule as a PolicyResult for
        business_id/intent. Unknown intents and missing rules fail closed to owner
        attention. Sort by descending priority then rule ID so equal-priority behavior is
        repeatable; never evaluate a different business's rules.
        """
        # Rule text is audit data, never executable instructions. Exact business
        # scoping and stable tie-breaking keep authorization independent of a model.
        matches = sorted(
            (r for r in rules if r.enabled and r.business_id == business_id and r.intent == intent),
            key=lambda r: (-r.priority, r.id),
        )
        if intent == Intent.UNKNOWN or not matches:
            return PolicyResult(
                intent,
                Action.REQUIRE_OWNER_ATTENTION,
                None,
                "UNKNOWN_INTENT" if intent == Intent.UNKNOWN else "POLICY_MISSING",
            )
        chosen = matches[0]
        return PolicyResult(
            intent,
            chosen.action,
            chosen.id,
            "OWNER_RULE_REQUIRES_ATTENTION"
            if chosen.action == Action.REQUIRE_OWNER_ATTENTION
            else None,
        )
