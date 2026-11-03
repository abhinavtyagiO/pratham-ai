from datetime import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.types import Action, BusinessContext, PolicyResult
from app.prompts.templates import PRICE_PHRASES, UNKNOWN_RESPONSE


def format_closing_time(value: time) -> str:
    hour = value.hour
    if hour < 6 or hour >= 20:
        period = "raat"
    elif hour < 12:
        period = "subah"
    elif hour < 16:
        period = "dopahar"
    else:
        period = "shaam"
    return f"{period} {hour % 12 or 12}:{value.minute:02d}"


class ModelContext(BaseModel):
    """An allowlist, never a filtered copy of an arbitrary business/customer payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Action
    closing_time_phrase: str | None = None


class PricePhrasing(StrEnum):
    POLITE = "POLITE"
    BRIEF = "BRIEF"


class ResponseDraft(BaseModel):
    """No free-text channel: the model selects approved speech acts only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    phrasing: PricePhrasing = PricePhrasing.POLITE
    mention_closing_time: bool = True


class SummaryContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: str
    decision: Action
    outcome: str


class SummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    acknowledged: bool = True


def build_model_context(business: BusinessContext, policy: PolicyResult) -> ModelContext:
    return ModelContext(
        decision=policy.decision,
        closing_time_phrase=(
            format_closing_time(business.closing_time)
            if policy.decision == Action.DO_NOT_DISCLOSE_PRICE
            else None
        ),
    )


def render_response(context: ModelContext, draft: ResponseDraft) -> str:
    if context.decision == Action.REQUIRE_OWNER_ATTENTION:
        return UNKNOWN_RESPONSE
    response = PRICE_PHRASES[draft.phrasing]
    if draft.mention_closing_time and context.closing_time_phrase:
        response += f" Shop ka band hone ka samay {context.closing_time_phrase} hai."
    return response


def render_summary(context: SummaryContext, draft: SummaryDraft) -> str:
    if not draft.acknowledged:
        raise ValueError("Summary provider did not acknowledge the result")
    if context.decision == Action.DO_NOT_DISCLOSE_PRICE:
        return (
            "Caller asked about tyre pricing. The PA followed the owner's no-price-on-call "
            "rule and invited the caller to visit the store. No price was disclosed."
        )
    return (
        "Caller query was recorded. Confirmed information was unavailable; owner attention needed."
    )
