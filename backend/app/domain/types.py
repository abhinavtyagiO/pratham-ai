"""Immutable domain facts, policy decisions, and bounded lifecycle vocabularies."""

from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from zoneinfo import ZoneInfo


class Intent(StrEnum):
    """Supported deterministic query classifications shared by simulation and live calls.
    Unsupported requests remain UNKNOWN rather than expanding MVP behavior implicitly.
    """

    PRICE_ENQUIRY = "PRICE_ENQUIRY"
    UNKNOWN = "UNKNOWN"


class Action(StrEnum):
    """Backend-authorized capabilities selected by owner policy, not by a response model. Both
    actions constrain speech to reviewed templates with no price disclosure.
    """

    DO_NOT_DISCLOSE_PRICE = "DO_NOT_DISCLOSE_PRICE"
    REQUIRE_OWNER_ATTENTION = "REQUIRE_OWNER_ATTENTION"


class Outcome(StrEnum):
    """Conservative operator-facing resolution classification. Live calls also require
    confirmed playback and complete turn processing before RESOLVED is possible.
    """

    RESOLVED = "RESOLVED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class CallStatus(StrEnum):
    """Persisted call lifecycle: active, completed, or failed. Outcome and attention are
    recorded separately so failure cannot imply a successfully answered query.
    """

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Speaker(StrEnum):
    """Transcript role vocabulary used by both orchestrators and persistence. Provider/system
    diagnostics are stored separately from customer and assistant messages.
    """

    CUSTOMER = "CUSTOMER"
    ASSISTANT = "ASSISTANT"


@dataclass(frozen=True)
class BusinessContext:
    """Immutable owner-confirmed business facts loaded by the repository. Contains local hours
    and timezone plus greeting facts; models receive only an explicitly constructed subset.
    No database Session or mutable ORM state is retained.
    """

    id: str
    name: str
    business_type: str
    address: str
    opening_time: time
    closing_time: time
    timezone: str
    default_language: str

    def __post_init__(self):
        """Validate the configured IANA timezone immediately by constructing ZoneInfo. Unknown
        zones raise ZoneInfoNotFoundError before invalid business context can enter a call;
        local hours are not converted here.
        """
        ZoneInfo(self.timezone)


@dataclass(frozen=True)
class OwnerRule:
    """Immutable business-scoped policy rule with intent/action, enablement, and priority. The
    deterministic engine uses structured fields; instruction is audit text, not executable
    model guidance.
    """

    id: str
    business_id: str
    intent: Intent
    action: Action
    instruction: str
    enabled: bool = True
    priority: int = 100


@dataclass(frozen=True)
class IntentResult:
    """Immutable classification result with optional measured confidence. The keyword detector
    leaves confidence unset rather than fabricating a probability.
    """

    intent: Intent
    confidence: float | None = None


@dataclass(frozen=True)
class PolicyResult:
    """Immutable authorization result including detected intent, chosen Action/rule, and
    optional owner-attention reason. It is persisted before model generation and defines the
    permitted ModelContext.
    """

    detected_intent: Intent
    decision: Action
    rule_id: str | None
    attention_reason: str | None = None


def classify_outcome(policy: PolicyResult) -> Outcome:
    """Map a legacy single-turn policy result to Outcome without consulting model wording.
    Real-time calls perform stricter aggregate classification that also checks delivery and
    failures.
    """
    if policy.decision == Action.DO_NOT_DISCLOSE_PRICE:
        return Outcome.RESOLVED
    return Outcome.NEEDS_ATTENTION
