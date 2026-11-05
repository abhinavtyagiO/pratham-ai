import asyncio

from sqlalchemy import func, select

from app.domain.types import CallStatus, Outcome, Speaker
from app.persistence.models import (
    Call,
    CallFailure,
    ConversationMessage,
    PolicyDecision,
    VoiceSession,
    VoiceTurn,
)
from app.persistence.repository import CallRepository


class VoiceStore:
    """Short transactions off the event loop; no Session is shared across tasks/threads/calls."""

    def __init__(self, sessions, call_id):
        self.sessions, self.call_id = sessions, call_id
        self.lock = asyncio.Lock()

    async def execute(self, operation):
        def transact():
            with self.sessions() as db:
                result = operation(db, CallRepository(db))
                db.commit()
                return result

        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(transact))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Database writes cannot be cancelled mid-transaction. Preserve ordering.
                await task
                raise

    async def start(self, business_id, info, trace_id, mode):
        def start(db, repo):
            repo.start(self.call_id, business_id, info.caller_phone, trace_id, commit=False)
            business, rules = repo.load_context(business_id)
            repo.save_snapshot(
                self.call_id,
                repo.snapshot(
                    business,
                    rules,
                    {
                        "voice": "voice_runtime:v1",
                        "response": "response_generation:v1",
                    },
                ),
                commit=False,
            )
            db.add(
                VoiceSession(
                    call_id=self.call_id,
                    provider_call_sid=info.call_sid,
                    stream_sid=info.stream_sid,
                    sample_rate=info.sample_rate,
                    mode=mode,
                )
            )
            return business

        return await self.execute(start)

    async def context(self):
        return await self.execute(
            lambda db, repo: repo.load_context(db.get(Call, self.call_id).business_id)
        )

    def _message(self, db, speaker, text, status, turn):
        sequence = db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.call_id == self.call_id)
        )
        message = ConversationMessage(
            call_id=self.call_id,
            speaker=speaker,
            text=text,
            delivery_status=status,
            turn_index=turn,
            sequence=sequence,
        )
        db.add(message)
        db.flush()
        return message.id

    async def customer(self, text, turn, *, partial=False):
        def save(db, repo):
            message_id = self._message(
                db, Speaker.CUSTOMER, text, "PARTIAL" if partial else "RECORDED", turn
            )
            if not partial:
                db.add(VoiceTurn(call_id=self.call_id, turn_index=turn, status="PROCESSING"))
            return message_id

        return await self.execute(save)

    async def assistant(self, text, turn):
        def save(db, repo):
            message_id = self._message(db, Speaker.ASSISTANT, text, "GENERATED", turn)
            row = db.scalar(
                select(VoiceTurn).where(
                    VoiceTurn.call_id == self.call_id, VoiceTurn.turn_index == turn
                )
            )
            if row:
                row.assistant_message_id = message_id
            return message_id

        return await self.execute(save)

    async def decision(self, policy, turn):
        def save(db, repo):
            db.add(
                PolicyDecision(
                    call_id=self.call_id,
                    rule_id=policy.rule_id,
                    detected_intent=policy.detected_intent,
                    decision=policy.decision,
                    turn_index=turn,
                )
            )
            row = db.scalar(
                select(VoiceTurn).where(
                    VoiceTurn.call_id == self.call_id, VoiceTurn.turn_index == turn
                )
            )
            row.outcome = (
                "RESOLVED" if policy.rule_id and not policy.attention_reason else "NEEDS_ATTENTION"
            )
            row.attention_reason = policy.attention_reason

        await self.execute(save)

    async def delivery(self, message_id, status):
        def save(db, repo):
            db.get(ConversationMessage, message_id).delivery_status = status
            row = db.scalar(select(VoiceTurn).where(VoiceTurn.assistant_message_id == message_id))
            if row:
                row.status = status

        await self.execute(save)

    async def invocation(self, **fields):
        await self.execute(lambda db, repo: repo.invocation(call_id=self.call_id, **fields))

    async def failure(self, trace_id, failure):
        def save(db, repo):
            db.add(
                CallFailure(
                    call_id=self.call_id,
                    trace_id=trace_id,
                    stage=failure.stage,
                    category=failure.category,
                    provider=None,
                    error_type=failure.error_type,
                    retryable=False,
                )
            )

        await self.execute(save)

    async def finish(self, reason, attention, *, failed=False):
        def save(db, repo):
            turns = list(db.scalars(select(VoiceTurn).where(VoiceTurn.call_id == self.call_id)))
            incomplete = any(t.status != "PLAYED" or t.outcome != "RESOLVED" for t in turns)
            needs_attention = bool(attention or incomplete or not turns)
            outcome = Outcome.NEEDS_ATTENTION if needs_attention else Outcome.RESOLVED
            summary = f"{len(turns)} customer turn(s) recorded. " + (
                "Owner attention required; review turn and playback states."
                if needs_attention
                else "Price enquiries handled under the no-price-on-call rule; "
                "playback acknowledged."
            )
            repo.complete(
                self.call_id,
                outcome,
                summary,
                attention or ("UNCONFIRMED_RESPONSE" if incomplete else "NO_QUERY")
                if needs_attention
                else None,
                commit=False,
            )
            if failed:
                db.get(Call, self.call_id).status = CallStatus.FAILED
            db.get(VoiceSession, self.call_id).end_reason = reason
            for row in turns:
                if row.status == "PROCESSING":
                    row.status = "UNCONFIRMED"
            for message in db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.call_id == self.call_id,
                    ConversationMessage.delivery_status == "GENERATED",
                )
            ):
                message.delivery_status = "UNCONFIRMED"
            return outcome

        return await self.execute(save)
