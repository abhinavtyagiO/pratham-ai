"""Synchronous call persistence and replay snapshots, composed by the live transaction store."""

from dataclasses import asdict
from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.types import (
    Action,
    BusinessContext,
    CallStatus,
    Intent,
    Outcome,
    OwnerRule,
    PolicyResult,
)
from app.persistence.models import (
    Business,
    BusinessRule,
    Call,
    CallFailure,
    CallOutcome,
    ConversationMessage,
    ModelInvocation,
    PolicyDecision,
    VoiceSession,
    VoiceTurn,
    utcnow,
)
from app.services.errors import BusinessNotFound


class CallRepository:
    """Synchronous persistence operations bound to one caller-owned SQLAlchemy Session. The
    legacy orchestrator commits stages for partial-record recovery; VoiceStore composes
    selected commit=False operations into atomic worker-thread transactions. Never share an
    instance/Session across concurrent calls or threads.
    """

    def __init__(self, session: Session):
        """Bind the transaction/request-owned Session without creating a connection or
        committing. The surrounding context must close it and decide rollback/disposal on
        failure.
        """
        self.session = session

    def load_context(self, business_id):
        """Load business_id and its rules into immutable domain objects. Raise BusinessNotFound
        for absent businesses and propagate invalid enum/timezone configuration; no live ORM
        objects leave the returned context/rule list.
        """
        business = self.session.get(Business, business_id)
        if business is None:
            raise BusinessNotFound(business_id)
        context = BusinessContext(
            **{k: getattr(business, k) for k in BusinessContext.__dataclass_fields__}
        )
        rules = [
            OwnerRule(
                id=r.id,
                business_id=r.business_id,
                intent=Intent(r.intent),
                action=Action(r.action),
                instruction=r.instruction,
                enabled=r.enabled,
                priority=r.priority,
            )
            for r in self.session.scalars(
                select(BusinessRule).where(BusinessRule.business_id == business_id)
            )
        ]
        return context, rules

    @staticmethod
    def snapshot(business, rules, versions):
        """Serialize confirmed business facts, structured rules, and supplied component
        versions for replay/audit. Convert local times to ISO strings; this full snapshot
        remains in the application database, not in model context.
        """
        data = asdict(business)
        data["opening_time"] = business.opening_time.isoformat()
        data["closing_time"] = business.closing_time.isoformat()
        return {"business": data, "rules": [asdict(r) for r in rules], "versions": versions}

    @staticmethod
    def restore_snapshot(snapshot):
        """Reconstruct immutable facts/rules from a saved snapshot, parsing local ISO times and
        enums. Invalid snapshot data raises rather than silently using current configuration
        during deterministic replay.
        """
        data = dict(snapshot["business"])
        for key in ("opening_time", "closing_time"):
            data[key] = time.fromisoformat(data[key])
        rules = [
            OwnerRule(**{**r, "intent": Intent(r["intent"]), "action": Action(r["action"])})
            for r in snapshot["rules"]
        ]
        return BusinessContext(**data), rules

    def start(self, call_id, business_id, caller_phone, trace_id, replay_of=None, *, commit=True):
        """Create an IN_PROGRESS call after confirming business existence, with optional replay
        linkage. Commit by default for legacy partial-record recovery; commit=False lets
        VoiceStore atomically add its session/snapshot rows.
        """
        if self.session.get(Business, business_id) is None:
            raise BusinessNotFound(business_id)
        self.session.add(
            Call(
                id=call_id,
                business_id=business_id,
                caller_phone=caller_phone,
                trace_id=trace_id,
                status=CallStatus.IN_PROGRESS,
                configuration_snapshot={},
                replay_of_call_id=replay_of,
            )
        )
        if commit:
            self.session.commit()

    def save_snapshot(self, call_id, snapshot, *, commit=True):
        """Assign the call's initial configuration snapshot and optionally commit. The live
        store disables this commit to keep initialization atomic with call/session creation.
        """
        self.session.get(Call, call_id).configuration_snapshot = snapshot
        if commit:
            self.session.commit()

    def message(self, call_id, speaker, text):
        """Append a sequenced legacy transcript message and commit it immediately. The
        synchronous orchestrator is the sole writer; live concurrent tasks use VoiceStore's
        serialized transaction path instead.
        """
        count = len(
            self.session.scalars(
                select(ConversationMessage.id).where(ConversationMessage.call_id == call_id)
            ).all()
        )
        self.session.add(
            ConversationMessage(
                call_id=call_id,
                speaker=speaker,
                text=text,
                sequence=count,
            )
        )
        self.session.commit()

    def decision(self, call_id, policy: PolicyResult):
        """Persist and commit the authoritative single-turn PolicyResult before model access.
        Live decisions add a turn index through VoiceStore rather than this legacy helper.
        """
        self.session.add(
            PolicyDecision(
                call_id=call_id,
                rule_id=policy.rule_id,
                detected_intent=policy.detected_intent,
                decision=policy.decision,
            )
        )
        self.session.commit()

    def invocation(self, **fields):
        """Persist and commit sanitized ModelInvocation keyword fields, including failure
        diagnostics. Callers must pass only safe identifier/schema metadata and validated
        outputs, never raw provider payloads.
        """
        self.session.add(ModelInvocation(**fields))
        self.session.commit()

    def complete(self, call_id, outcome, summary, attention_reason, *, commit=True):
        """Set completed lifecycle timestamps and create the aggregate outcome/summary. Commit
        by default; live finalization uses commit=False so session reason and unfinished
        delivery updates are part of one transaction.
        """
        call = self.session.get(Call, call_id)
        call.status = CallStatus.COMPLETED
        call.ended_at = utcnow()
        self.session.add(
            CallOutcome(
                call_id=call_id,
                outcome=outcome,
                summary=summary,
                needs_attention=outcome == Outcome.NEEDS_ATTENTION,
                attention_reason=attention_reason,
            )
        )
        if commit:
            self.session.commit()

    def fail(self, call_id, trace_id, stage, category, provider, error_type, retryable):
        """Rollback the failed operation, then persist FAILED status, safe diagnostics, and
        NEEDS_ATTENTION for an existing call. Return False if call creation never committed.
        Retryability is recorded only; raw exception strings are never persisted.
        """
        self.session.rollback()
        call = self.session.get(Call, call_id)
        if call is None:
            return False
        call.status = CallStatus.FAILED
        call.ended_at = utcnow()
        self.session.add(
            CallFailure(
                call_id=call_id,
                trace_id=trace_id,
                stage=stage,
                category=category,
                provider=provider,
                error_type=error_type,
                retryable=retryable,
            )
        )
        outcome = self.session.get(CallOutcome, call_id)
        if outcome is None:
            outcome = CallOutcome(call_id=call_id)
            self.session.add(outcome)
        outcome.outcome = Outcome.NEEDS_ATTENTION
        outcome.needs_attention = True
        outcome.summary = f"Call processing failed at {stage}. Owner attention required."
        outcome.attention_reason = str(category)
        self.session.commit()
        return True

    def detail(self, call_id):
        """Return the full application call aggregate or None, including ordered
        transcript/turns, policy audits, safe model diagnostics, failures, and voice session
        metadata. This contains private transcript text and is exposed only through
        authenticated data routes.
        """
        call = self.session.get(Call, call_id)
        if call is None:
            return None

        def fields(row):
            """Convert mapped scalar columns to a response dictionary without traversing ORM
            relationships or leaking Session objects.
            """
            return {c.name: getattr(row, c.name) for c in row.__table__.columns}

        outcome = self.session.get(CallOutcome, call_id)
        return {
            **fields(call),
            "voice_session": fields(voice)
            if (voice := self.session.get(VoiceSession, call_id))
            else None,
            "voice_turns": [
                fields(r)
                for r in self.session.scalars(
                    select(VoiceTurn)
                    .where(VoiceTurn.call_id == call_id)
                    .order_by(VoiceTurn.turn_index)
                )
            ],
            "outcome": fields(outcome) if outcome else None,
            "messages": [
                fields(r)
                for r in self.session.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.call_id == call_id)
                    .order_by(ConversationMessage.sequence)
                )
            ],
            "policy_decisions": [
                fields(r)
                for r in self.session.scalars(
                    select(PolicyDecision)
                    .where(PolicyDecision.call_id == call_id)
                    .order_by(PolicyDecision.decision_timestamp)
                )
            ],
            "model_invocations": [
                fields(r)
                for r in self.session.scalars(
                    select(ModelInvocation).where(ModelInvocation.call_id == call_id)
                )
            ],
            "failures": [
                fields(r)
                for r in self.session.scalars(
                    select(CallFailure).where(CallFailure.call_id == call_id)
                )
            ],
        }
