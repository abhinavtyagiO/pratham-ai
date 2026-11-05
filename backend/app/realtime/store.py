"""Durable live-call state with per-call ordering and thread-local SQLAlchemy sessions."""

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
    """Persist one live call using short independent SQLAlchemy transactions off the event
    loop. Own a call ID and per-call async lock, not a long-lived Session. execute() creates
    a new Session inside its worker thread; shielded writes finish before cancellation
    releases ordering. Different calls have independent locks and transactions.
    """

    def __init__(self, sessions, call_id):
        """Bind a thread-safe session factory and application call ID, and allocate the per-
        call transaction lock. Do not pass a shared SQLAlchemy Session; database objects are
        created only inside execute().
        """
        self.sessions, self.call_id = sessions, call_id
        self.lock = asyncio.Lock()

    async def execute(self, operation):
        """Run operation(db, repository) in a fresh worker-thread Session and commit its result
        under the per-call lock. Context exit rolls back failures. Shield the worker so
        cancellation waits for its transaction before re-raising; this prevents an abandoned
        write from overtaking later delivery/finalization writes.
        """

        def transact():
            """Own the thread-local Session, call the operation, commit, and return its value.
            Session context cleanup rolls back exceptions; no live Session crosses back to
            the event loop.
            """
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

    async def start(
        self, business_id, info, trace_id, mode, response_version="response_generation:v1"
    ):
        """Atomically create the call, immutable initial configuration snapshot, and unique
        VoiceSession identifiers. Return detached BusinessContext for the greeting; missing
        business or uniqueness/database failures propagate before provider startup.
        """

        def start(db, repo):
            """Create the initial rows in one caller-owned transaction and return business
            facts. Defer repository commits so a failed voice-session insert cannot leave a
            partially initialized call.
            """
            repo.start(self.call_id, business_id, info.caller_phone, trace_id, commit=False)
            business, rules = repo.load_context(business_id)
            repo.save_snapshot(
                self.call_id,
                repo.snapshot(
                    business,
                    rules,
                    {
                        "voice": "voice_runtime:v1",
                        "response": response_version,
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
        """Reload current business facts and owner rules for the call's business in a short
        transaction. Returning immutable domain values avoids sharing ORM state between the
        response task and database thread.
        """
        return await self.execute(
            lambda db, repo: repo.load_context(db.get(Call, self.call_id).business_id)
        )

    def _message(self, db, speaker, text, status, turn):
        """Append a sequenced transcript row inside the already-held per-call transaction.
        Flush to obtain its ID, but leave commit to execute(); callers provide the speaker,
        delivery state, and turn index.
        """
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
        """Persist final customer text plus a PROCESSING turn, or a PARTIAL transcript without
        a new turn on shutdown. Return the message ID after commit; transcript content stays
        in the application database.
        """

        def save(db, repo):
            """Insert the customer row and create VoiceTurn only for a final transcript. Keep
            these changes atomic so a durable final cannot lack its processing turn.
            """
            message_id = self._message(
                db, Speaker.CUSTOMER, text, "PARTIAL" if partial else "RECORDED", turn
            )
            if not partial:
                db.add(VoiceTurn(call_id=self.call_id, turn_index=turn, status="PROCESSING"))
            return message_id

        return await self.execute(save)

    async def assistant(self, text, turn):
        """Persist rendered authorized text as GENERATED and associate it with its customer
        turn. Return the message ID for later playback marks; generation alone is not
        delivery confirmation.
        """

        def save(db, repo):
            """Insert the assistant transcript and link an existing turn in one transaction.
            Greeting/recovery turn zero has no customer VoiceTurn to update.
            """
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
        """Persist the policy audit and expected turn outcome before requesting model phrasing.
        A matched safe rule can resolve the query, but final call resolution still requires
        acknowledged playback.
        """

        def save(db, repo):
            """Insert a turn-indexed decision and update the corresponding turn's policy
            outcome/attention reason. Database failures abort both changes rather than
            recording inconsistent authorization.
            """
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
        """Atomically update transcript and linked turn delivery state after ACK or
        interruption. The session orders competing in-memory transitions before awaiting
        this serialized write.
        """

        def save(db, repo):
            """Apply the same delivery status to the assistant message and its linked customer
            turn. Greeting messages have no turn; execute() owns the commit.
            """
            db.get(ConversationMessage, message_id).delivery_status = status
            row = db.scalar(select(VoiceTurn).where(VoiceTurn.assistant_message_id == message_id))
            if row:
                row.status = status

        await self.execute(save)

    async def invocation(self, **fields):
        """Persist safe model diagnostics correlated with this call, including cancelled/failed
        requests. Raw model input and transcript are excluded; repository invocation commits
        the diagnostic inside the serialized operation.
        """
        await self.execute(lambda db, repo: repo.invocation(call_id=self.call_id, **fields))

    async def failure(self, trace_id, failure, *, provider=None):
        """Persist bounded category/stage/type diagnostics and optional provider identity. Mark
        known timeout/connection types retryable for operators, without automatically
        retrying speech.
        """

        def save(db, repo):
            """Create a CallFailure row using static codes and exception type only. Do not
            persist raw provider error messages, headers, or tokens.
            """
            db.add(
                CallFailure(
                    call_id=self.call_id,
                    trace_id=trace_id,
                    stage=failure.stage,
                    category=failure.category,
                    provider=provider,
                    error_type=failure.error_type,
                    retryable=failure.error_type
                    in {"TimeoutError", "ConnectionError", "ConnectionClosedError"},
                )
            )

        await self.execute(save)

    async def finish(self, reason, attention, *, failed=False):
        """Atomically finalize call/session outcome and convert unfinished work to unconfirmed.
        Any attention reason, missing turn, interrupted/unplayed reply, or unresolved policy
        keeps NEEDS_ATTENTION; return the aggregate Outcome after commit.
        """

        def save(db, repo):
            """Compute conservative outcome, write deterministic summary/end reason, and close
            incomplete delivery states in one transaction. Preserve FAILED call status when
            requested; GENERATED messages cannot remain apparently pending after cleanup.
            """
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
