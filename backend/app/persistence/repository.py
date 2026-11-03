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
    utcnow,
)
from app.services.errors import BusinessNotFound


class CallRepository:
    def __init__(self, session: Session):
        self.session = session

    def load_context(self, business_id):
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
        data = asdict(business)
        data["opening_time"] = business.opening_time.isoformat()
        data["closing_time"] = business.closing_time.isoformat()
        return {"business": data, "rules": [asdict(r) for r in rules], "versions": versions}

    @staticmethod
    def restore_snapshot(snapshot):
        data = dict(snapshot["business"])
        for key in ("opening_time", "closing_time"):
            data[key] = time.fromisoformat(data[key])
        rules = [
            OwnerRule(**{**r, "intent": Intent(r["intent"]), "action": Action(r["action"])})
            for r in snapshot["rules"]
        ]
        return BusinessContext(**data), rules

    def start(self, call_id, business_id, caller_phone, trace_id, replay_of=None):
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
        self.session.commit()

    def save_snapshot(self, call_id, snapshot):
        self.session.get(Call, call_id).configuration_snapshot = snapshot
        self.session.commit()

    def message(self, call_id, speaker, text):
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
        self.session.add(ModelInvocation(**fields))
        self.session.commit()

    def complete(self, call_id, outcome, summary, attention_reason):
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
        self.session.commit()

    def fail(self, call_id, trace_id, stage, category, provider, error_type, retryable):
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
        call = self.session.get(Call, call_id)
        if call is None:
            return None

        def fields(row):
            return {c.name: getattr(row, c.name) for c in row.__table__.columns}

        outcome = self.session.get(CallOutcome, call_id)
        return {
            **fields(call),
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
