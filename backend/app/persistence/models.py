from datetime import UTC, datetime, time
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Time
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow():
    return datetime.now(UTC)


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """SQLite drops offsets; restore UTC so both databases return aware timestamps."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone aware")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Business(Base):
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
    __tablename__ = "business_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    intent: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(50))
    instruction: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class Call(Base):
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
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CallOutcome(Base):
    __tablename__ = "call_outcomes"
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    needs_attention: Mapped[bool] = mapped_column(Boolean)
    attention_reason: Mapped[str | None] = mapped_column(String(100))


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("business_rules.id"))
    detected_intent: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(50))
    decision_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ModelInvocation(Base):
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
