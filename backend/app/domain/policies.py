from collections.abc import Sequence

from app.domain.types import Action, Intent, OwnerRule, PolicyResult


class PolicyEngine:
    """Owner rules decide capabilities; models cannot introduce new capabilities."""

    version = "owner_policy:v1"

    def evaluate(
        self, business_id: str, intent: Intent, rules: Sequence[OwnerRule]
    ) -> PolicyResult:
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
