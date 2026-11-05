"""Relational business, call, policy, model-audit, and voice-delivery records."""

from datetime import UTC, datetime, time
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow():
    """Return an aware UTC timestamp for persisted lifecycle/audit defaults. Wall-clock values
    are used for records; runtime deadlines use monotonic clocks separately.
    """
    return datetime.now(UTC)


def new_id():
    """Return a random UUID string for application-owned primary keys, independent of provider
    call or stream identifiers.
    """
    return str(uuid4())


class Base(DeclarativeBase):
    """SQLAlchemy declarative registry shared by application tables and Alembic metadata
    checks. It owns table definitions, not transaction/session state; migrations explicitly
    create and remove the schema.
    """

    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware SQLAlchemy type normalizing timestamps to UTC across SQLite and
    PostgreSQL. SQLite drops offsets, so result conversion restores UTC; naive input is
    rejected. Instances are cache-safe and contain no per-call mutable state.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Return an aware UTC datetime or None before database binding. Reject naive
        timestamps with ValueError instead of guessing a timezone; dialect is supplied by
        SQLAlchemy.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone aware")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        """Return stored timestamps normalized to aware UTC, restoring SQLite's missing offset.
        Preserve None for unfinished lifecycle timestamps.
        """
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Business(Base):
    """Owner-maintained business facts used for greetings, local hours, and policy context.
    Rows are loaded into immutable BusinessContext before leaving a transaction; response
    models never receive this entire record.
    """

    __tablename__ = "businesses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    business_type: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(Text)
    opening_time: Mapped[time] = mapped_column(Time)
    closing_time: Mapped[time] = mapped_column(Time)
    timezone: Mapped[str] = mapped_column(String(100))
    default_language: Mapped[str] = mapped_column(String(30))


class BusinessRule(Base):
    """Persistent business-scoped structured owner policy with enablement and stable priority.
    PolicyEngine interprets intent/action fields deterministically; instruction text remains
    an audit aid, not an executable model prompt.
    """

    __tablename__ = "business_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    intent: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(50))
    instruction: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class Call(Base):
    """Application call identity, lifecycle timestamps, correlation trace, and initial
    configuration snapshot. References one business and optionally an earlier simulated call
    for replay. Live VoiceSession holds transport-specific state separately; updates require
    an owning transaction.
    """

    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    caller_phone: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    # Immutable call-time facts and rules for replay, kept in the application database.
    configuration_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    replay_of_call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"))


class ConversationMessage(Base):
    """Ordered durable customer/assistant transcript with turn association and delivery state.
    GENERATED does not imply heard; only Exotel ACK permits PLAYED, and cleanup marks
    unfinished output UNCONFIRMED. Raw text belongs in this protected database rather than
    operational logs.
    """

    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    delivery_status: Mapped[str] = mapped_column(
        String(30), default="RECORDED", server_default="RECORDED"
    )
    turn_index: Mapped[int | None] = mapped_column(Integer)


class CallOutcome(Base):
    """One aggregate result and deterministic summary per call. needs_attention captures
    unresolved policy, failure, missing query, or uncertain playback independently of the
    Call lifecycle status.
    """

    __tablename__ = "call_outcomes"
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    needs_attention: Mapped[bool] = mapped_column(Boolean)
    attention_reason: Mapped[str | None] = mapped_column(String(100))


class PolicyDecision(Base):
    """Timestamped audit of classified intent, authoritative action, selected owner rule, and
    optional live turn index. Persist before generation so speech authorization remains
    reviewable even if a provider later fails.
    """

    __tablename__ = "policy_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("business_rules.id"))
    detected_intent: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(50))
    decision_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    turn_index: Mapped[int | None] = mapped_column(Integer)


class ModelInvocation(Base):
    """Safe model diagnostics with version, identifier-only input references, validated output,
    usage, latency, and error type. Raw prompts, transcripts, headers, and arbitrary
    provider responses are intentionally excluded.
    """

    __tablename__ = "model_invocations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    trace_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_context_identifiers: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    token_usage: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(100))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class CallFailure(Base):
    """Durable sanitized failure category/stage/type with provider identity and retryability
    hint. Contains no exception message or credentials; retryable is an operator signal, not
    automatic speech replay behavior.
    """

    __tablename__ = "call_failures"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    trace_id: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str] = mapped_column(String(100))
    retryable: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class VoiceSession(Base):
    """One-to-one Exotel transport record containing unique provider call/stream IDs,
    negotiated sample rate, provider mode, and terminal reason. Unique constraints guard
    duplicate durable identities in addition to process-local admission.
    """

    __tablename__ = "voice_sessions"
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), primary_key=True)
    provider_call_sid: Mapped[str] = mapped_column(String(128), unique=True)
    stream_sid: Mapped[str] = mapped_column(String(128), unique=True)
    sample_rate: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20))
    end_reason: Mapped[str | None] = mapped_column(String(40))


class VoiceTurn(Base):
    """One final customer turn with processing/delivery state, policy outcome, and assistant
    transcript link. Unique call/turn indexing keeps multi-turn evidence ordered;
    INTERRUPTED or UNCONFIRMED prevents aggregate resolution even when later turns play
    successfully.
    """

    __tablename__ = "voice_turns"
    __table_args__ = (UniqueConstraint("call_id", "turn_index"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    outcome: Mapped[str | None] = mapped_column(String(30))
    assistant_message_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_messages.id"))
    attention_reason: Mapped[str | None] = mapped_column(String(60))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
