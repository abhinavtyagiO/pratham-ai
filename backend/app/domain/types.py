from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from zoneinfo import ZoneInfo


class Intent(StrEnum):
    PRICE_ENQUIRY = "PRICE_ENQUIRY"
    UNKNOWN = "UNKNOWN"


class Action(StrEnum):
    DO_NOT_DISCLOSE_PRICE = "DO_NOT_DISCLOSE_PRICE"
    REQUIRE_OWNER_ATTENTION = "REQUIRE_OWNER_ATTENTION"


class Outcome(StrEnum):
    RESOLVED = "RESOLVED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class CallStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Speaker(StrEnum):
    CUSTOMER = "CUSTOMER"
    ASSISTANT = "ASSISTANT"


@dataclass(frozen=True)
class BusinessContext:
    id: str
    name: str
    business_type: str
    address: str
    opening_time: time
    closing_time: time
    timezone: str
    default_language: str

    def __post_init__(self):
        ZoneInfo(self.timezone)


@dataclass(frozen=True)
class OwnerRule:
    id: str
    business_id: str
    intent: Intent
    action: Action
    instruction: str
    enabled: bool = True
    priority: int = 100


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float | None = None


@dataclass(frozen=True)
class PolicyResult:
    detected_intent: Intent
    decision: Action
    rule_id: str | None
    attention_reason: str | None = None


def classify_outcome(policy: PolicyResult) -> Outcome:
    if policy.decision == Action.DO_NOT_DISCLOSE_PRICE:
        return Outcome.RESOLVED
    return Outcome.NEEDS_ATTENTION
