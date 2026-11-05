"""Capability-limited model schemas and deterministic speech/summary rendering."""

from datetime import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.types import Action, BusinessContext, PolicyResult
from app.prompts.templates import PRICE_PHRASES, UNKNOWN_RESPONSE


def format_closing_time(value: time) -> str:
    """Render an owner-confirmed wall-clock time as a Hindi/Hinglish period and 12-hour time.
    This pure formatter does not convert timezones or infer opening hours; the business
    context supplies the authoritative local time.
    """
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
    """Immutable allowlist passed from deterministic policy evaluation to response models.
    Contains only the authorized Action and optional confirmed closing-time phrase, never a
    filtered arbitrary business/customer payload. Extra fields are rejected; no prices,
    inventory, caller instructions, or mutable state cross this boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Action
    closing_time_phrase: str | None = None


class PricePhrasing(StrEnum):
    """Closed vocabulary of reviewed response-template choices. A model may select POLITE or
    BRIEF, but cannot create a sentence or widen the policy capability.
    """

    POLITE = "POLITE"
    BRIEF = "BRIEF"


class ResponseDraft(BaseModel):
    """Immutable, extra-forbidden model result selecting approved phrasing and optional
    closing-time mention. There is no free-text channel; the renderer combines these choices
    with authoritative ModelContext. Pydantic validation is required before any provider
    output reaches TTS.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    phrasing: PricePhrasing = PricePhrasing.POLITE
    mention_closing_time: bool = True


class SummaryContext(BaseModel):
    """Immutable allowlisted summary input containing classified intent, policy Action, and
    outcome only. It excludes raw transcripts and business records so the summary provider
    cannot invent customer facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: str
    decision: Action
    outcome: str


class SummaryDraft(BaseModel):
    """Immutable acknowledgment schema used by the legacy summary model. Extra fields are
    rejected; the backend renders the summary rather than accepting model-written text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    acknowledged: bool = True


def build_model_context(business: BusinessContext, policy: PolicyResult) -> ModelContext:
    """Return the model's minimal authorized context from confirmed business facts and a
    PolicyResult. Include closing time only for DO_NOT_DISCLOSE_PRICE; owner-attention
    decisions receive no business fact disclosure capability. Do not pass the original
    utterance or full records onward.
    """
    # Construct a fresh allowlist instead of subtracting forbidden fields from an
    # expanding business payload; future schema fields cannot leak by default.
    return ModelContext(
        decision=policy.decision,
        closing_time_phrase=(
            format_closing_time(business.closing_time)
            if policy.decision == Action.DO_NOT_DISCLOSE_PRICE
            else None
        ),
    )


def render_response(context: ModelContext, draft: ResponseDraft) -> str:
    """Render reviewed Hinglish wording from validated context/draft without model free text.
    Owner-attention policy overrides phrasing; optional closing time comes only from
    context. This pure function is shared by Slice 1 and the mock live-session path.
    """
    if context.decision == Action.REQUIRE_OWNER_ATTENTION:
        return UNKNOWN_RESPONSE
    response = PRICE_PHRASES[draft.phrasing]
    if draft.mention_closing_time and context.closing_time_phrase:
        response += f" Shop ka band hone ka samay {context.closing_time_phrase} hai."
    return response


def render_summary(context: SummaryContext, draft: SummaryDraft) -> str:
    """Render a deterministic legacy call summary from authorized context after provider
    acknowledgment. Raise ValueError if the draft declines acknowledgment; never use
    arbitrary summary text from a provider.
    """
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
