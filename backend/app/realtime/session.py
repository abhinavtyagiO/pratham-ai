"""Per-call asynchronous orchestration and lifecycle ownership for the live voice path."""

import asyncio
import hashlib
from collections import deque
from contextlib import aclosing
from time import monotonic
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode
from sqlalchemy.exc import SQLAlchemyError
from starlette.websockets import WebSocketDisconnect

from app.domain.conversations import ResponseDraft, build_model_context, render_response
from app.domain.policies import PolicyEngine
from app.observability.telemetry import event
from app.prompts.templates import GREETING, RESPONSE_VERSION
from app.providers.mock import DeterministicIntentDetector
from app.realtime.audio import EnergyVAD, PCMBuffer, PCMResampler
from app.realtime.contracts import SpeechEvent, VoiceFailure
from app.realtime.pipeline import AuthorizedPlayback, IncomingPCM, RecognizedSpeech, VoicePipeline
from app.realtime.playback import Packet, Playback
from app.realtime.speech_text import GREETING_HI, render_hindi
from app.realtime.store import VoiceStore

SILENCE_PROMPT = "क्या आप मुझे सुन पा रहे हैं? कृपया अपनी बात बताइए।"


class RealtimeSession:
    """Coordinate one authenticated Exotel call through policy-controlled speech turns.

    The router supplies an initialized protocol, fresh per-call providers, a session
    factory, and shared telemetry. This object owns its Pipecat worker, PCM resamplers, VAD,
    bounded playback, response task, and short-transaction VoiceStore. All mutable turn,
    generation, partial, delivery, and deadline state belongs to this call. Concurrent
    receive, recognition, playback, and watchdog tasks share one event loop; database work
    uses independent thread-local sessions. run() owns startup and cleanup, including
    cancellation propagation and conservative final outcomes.
    """

    def __init__(self, socket, protocol, settings, providers, sessions, telemetry):
        """Allocate per-call state without connecting providers or writing records. The
        protocol must already contain validated StartInfo. Input is normalized to 16 kHz
        mono PCM; generation 0 is reserved for the greeting. Resources become active only
        when run() starts.
        """
        self.socket, self.protocol, self.settings = socket, protocol, settings
        self.providers, self.telemetry = providers, telemetry
        self.call_id = str(uuid4())
        self.store = VoiceStore(sessions, self.call_id)
        self.pipeline = VoicePipeline(self)
        self.playback = Playback(socket, protocol, settings, self.first_sent, self.fail)
        self.resampler = PCMResampler(protocol.start.sample_rate, 16000)
        self.frames = PCMBuffer(640)
        self.vad = EnergyVAD(settings.energy_rms, settings.min_speech_ms, settings.silence_ms)
        self.preroll = deque(maxlen=15)
        self.generation = 0
        self.turn = 0
        self.response_task = None
        self.attempts = {}
        self.final_ids = set()
        self.partial = ""
        self.in_speech = False
        self.awaiting_final = None
        self.speech_since = None
        self.stopping = asyncio.Event()
        self.failure = None
        self.attention = None
        self.reason = "STOPPED"
        self.prompted = False
        self.last_activity = self.last_media = self.started = monotonic()
        self.sent_at = {}

    def log(self, name, **safe):
        """Emit a correlated structured event using the call's root trace. Keyword fields must
        be safe identifiers, enums, lengths, hashes, or measurements; this helper does not
        redact arbitrary text supplied by a caller.
        """
        event(name, self.call_id, self.trace_id, **safe)

    def latency(self, name, since, **safe):
        """Record elapsed milliseconds since a monotonic timestamp in both events and the
        voice-stage histogram. Additional safe fields identify the turn or generation
        without exposing speech.
        """
        elapsed = round((monotonic() - since) * 1000, 3)
        self.log(name, latency_ms=elapsed, **safe)
        self.telemetry.histograms["voice_stage_latency_ms"].record(elapsed, {"stage": name})

    def first_sent(self, generation):
        """Record when Playback first writes a generation's PCM packet to Exotel. This callback
        measures socket-send latency from response start, not carrier playback or caller
        comprehension.
        """
        self.sent_at[generation] = monotonic()
        self.latency("first_audio_sent", self.response_started, generation=generation)

    def fail(self, failure):
        """Latch the first VoiceFailure, mark owner attention, and wake run() for shutdown.
        Later failures cannot replace the originating category; task cancellation and
        persistence are handled by run().
        """
        if self.failure is None:
            self.failure = failure
            self.attention = failure.category
            self.reason = "FAILED"
            self.stopping.set()

    async def checked(self, stage, coroutine, category):
        """Await one provider operation under the configured stall deadline and return its
        value. Preserve VoiceFailure codes, translate other errors to category/stage/type
        only, and propagate CancelledError so barge-in can stop the operation.
        """
        try:
            async with asyncio.timeout(self.settings.provider_timeout_s):
                return await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, VoiceFailure):
                raise
            raise VoiceFailure(category, stage, type(exc).__name__) from None

    async def submit(self, frame):
        """Submit an acknowledged Pipecat frame with the provider deadline. Waiting for
        completion bounds in-flight work and propagates processor errors to the submitting
        session task.
        """
        await self.pipeline.submit(frame, self.settings.provider_timeout_s)

    def future(self):
        """Allocate a frame-completion future on the current event loop. The frame processor
        resolves it; timeout/cancellation can cancel it before queued work executes.
        """
        return asyncio.get_running_loop().create_future()

    async def audio(self, pcm):
        """Consume one 20 ms, 16 kHz PCM16 frame from Pipecat. Server-VAD mode forwards every
        frame, including silence. Energy mode maintains 300 ms pre-roll, emits manual speech
        boundaries, and sends speech through STT; provider errors propagate to the pipeline
        submitter.
        """
        if self.stopping.is_set():
            return
        if self.settings.vad_mode == "sarvam":
            # The server must hear pauses and background silence to own endpointing.
            # Running local VAD here as well would truncate words or split turns twice.
            await self.checked("stt_audio", self.providers.stt.audio(pcm), "STT_ERROR")
            return
        self.preroll.append(pcm)
        boundary = self.vad.update(pcm)
        if boundary == "speech_start":
            await self.speech(SpeechEvent("speech_start"))
            await self.checked("stt_boundary", self.providers.stt.boundary(boundary), "STT_ERROR")
            for buffered in self.preroll:
                await self.checked("stt_audio", self.providers.stt.audio(buffered), "STT_ERROR")
            self.preroll.clear()
        elif self.vad.active or boundary == "speech_end":
            await self.checked("stt_audio", self.providers.stt.audio(pcm), "STT_ERROR")
        if boundary == "speech_end":
            await self.speech(SpeechEvent("speech_end"))
            await self.checked("stt_boundary", self.providers.stt.boundary(boundary), "STT_ERROR")

    async def interrupt(self, reason):
        """Invalidate obsolete generation, cancel/join the response task, and flush playback
        when necessary. The monotonically increasing generation rejects late packets and
        cleared marks. Generated but unacknowledged replies become INTERRUPTED; ACKs
        received during lock waits retain PLAYED. Cancellation diagnostics finish their
        ordered database write before replacement speech proceeds.
        """
        tick = monotonic()
        # Invalidation happens before any await: late provider/pipeline output loses
        # its right to play even while cancellation waits for diagnostic persistence.
        self.generation += 1
        active = self.response_task
        was_generating = bool(active and not active.done())
        if active and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        self.response_task = None
        unplayed = [(g, value) for g, value in self.attempts.items() if value[1] == "GENERATED"]
        interrupted = bool(was_generating or unplayed or self.playback.pending)
        if interrupted:
            await self.playback.clear(self.generation)
            self.latency("audio_cleared", tick, reason=reason, generation=self.generation)
            for gen, (message_id, _) in unplayed:
                # A valid ACK may have arrived while clear waited for the send lock.
                # Delivery is decided at ACK receipt, not at its later database commit.
                if self.attempts[gen][1] != "GENERATED":
                    continue
                self.attempts[gen] = (message_id, "INTERRUPTED")
                await self.store.delivery(message_id, "INTERRUPTED")
        else:
            self.playback.generation = self.generation
        if interrupted:
            self.latency("barge_in_cancelled", tick, generation=self.generation)

    async def speech(self, speech):
        """Apply one normalized STT/VAD event to this conversation. Speech start cancels
        obsolete output, speech end starts the final-transcript deadline, and partials
        update bounded recovery state without invoking the model. A nonempty, nonduplicate
        final persists a customer turn and spawns respond(); transcript/turn limits fail
        closed.
        """
        now = monotonic()
        if self.stopping.is_set():
            return
        if speech.kind == "speech_start":
            self.last_activity = now
            self.in_speech = True
            self.speech_since = now
            self.prompted = False
            await self.interrupt("caller_speech")
            self.log("caller_speech_started")
        elif speech.kind == "speech_end":
            self.in_speech = False
            self.speech_since = None
            self.awaiting_final = now
            self.log("caller_speech_ended")
        elif speech.kind == "partial":
            if not self.in_speech and self.awaiting_final is None:
                await self.speech(SpeechEvent("speech_start"))
            self.partial = speech.text[:4000]
            self.last_activity = now
            self.log("stt_partial", text_length=len(speech.text))
        elif speech.kind == "final":
            if speech.utterance_id and speech.utterance_id in self.final_ids:
                return
            if not speech.text.strip():
                self.awaiting_final = None
                return
            if len(speech.text) > 4000 or self.turn >= 128:
                raise VoiceFailure("STT_ERROR", "transcript_limit")
            if speech.utterance_id:
                self.final_ids.add(speech.utterance_id)
            if self.awaiting_final:
                self.latency("stt_final", self.awaiting_final)
            else:
                self.log("stt_final", latency_ms=None)
            self.awaiting_final = None
            self.in_speech = False
            self.speech_since = None
            self.partial = ""
            self.last_activity = now
            await self.interrupt("new_final")
            self.turn += 1
            await self.store.customer(speech.text, self.turn)
            self.log("message_persisted", speaker="CUSTOMER", turn_index=self.turn)
            self.response_task = asyncio.create_task(
                self.respond(self.turn, self.generation, speech.text)
            )

    async def respond(self, turn, generation, utterance=None, fixed_text=None):
        """Generate and queue one policy-authorized response, or speak a reviewed fixed
        greeting/prompt. For normal turns, reload business/rules, detect intent, persist
        policy, allowlist ModelContext, validate the whole model draft, and render approved
        wording before TTS. Model failure uses a deterministic authorized fallback and owner
        attention. PCM is validated, resampled, packetized, and tagged with
        generation/message IDs. Cancellation propagates; other failures latch session
        shutdown, and diagnostic writes preserve cancelled invocations.
        """
        self.response_started = monotonic()
        stage = "load_business_context"
        version = getattr(self.providers.model, "prompt_version", RESPONSE_VERSION)
        try:
            async with asyncio.timeout(self.settings.response_timeout_s):
                if fixed_text is None:
                    with self.telemetry.stage("load_business_context", self.call_id):
                        business, rules = await self.store.context()
                    with self.telemetry.stage("detect_intent", self.call_id, "intent_latency_ms"):
                        detected = DeterministicIntentDetector().detect(utterance)
                        self.log("intent_detected", intent=detected.intent, turn_index=turn)
                        self.telemetry.count("intents_detected_total", intent=detected.intent)
                        if detected.intent == "UNKNOWN":
                            self.telemetry.count("unknown_intent_total")
                    with self.telemetry.stage("evaluate_policy", self.call_id, "policy_latency_ms"):
                        policy = PolicyEngine().evaluate(business.id, detected.intent, rules)
                        self.log(
                            "policy_evaluated",
                            decision=policy.decision,
                            rule_id=policy.rule_id,
                            turn_index=turn,
                        )
                        if policy.rule_id:
                            self.telemetry.count("rules_triggered_total", decision=policy.decision)
                    await self.store.decision(policy, turn)
                    with self.telemetry.stage("build_model_context", self.call_id):
                        context = build_model_context(business, policy)
                    tick = monotonic()
                    stage = "generate_response"
                    output, error, tokens = None, None, {"input": None, "output": None}
                    self.log(
                        "model_request_started",
                        prompt_version=version,
                        provider=self.providers.model.name,
                        model=self.providers.model.model,
                    )
                    try:
                        with self.telemetry.stage("generate_response", self.call_id):
                            result = await self.checked(
                                "generate_response",
                                self.providers.model.generate(context),
                                "MODEL_ERROR",
                            )
                            draft = ResponseDraft.model_validate(result.value)
                        output = draft.model_dump(mode="json")
                        tokens = {"input": result.input_tokens, "output": result.output_tokens}
                        self.latency("llm_first_usable_response", tick, turn_index=turn)
                    except asyncio.CancelledError:
                        await self.store.invocation(
                            trace_id=self.trace_id,
                            provider=self.providers.model.name,
                            model=self.providers.model.model,
                            prompt_version=version,
                            input_context_identifiers={
                                "business_id": business.id,
                                "rule_id": policy.rule_id,
                                "turn_index": turn,
                            },
                            output=None,
                            latency_ms=round((monotonic() - tick) * 1000),
                            token_usage=tokens,
                            error="CancelledError",
                            retry_count=0,
                        )
                        self.log(
                            "model_request_cancelled",
                            prompt_version=version,
                            provider=self.providers.model.name,
                            generation=generation,
                        )
                        raise
                    except (VoiceFailure, ValueError) as exc:
                        error = (
                            exc.error_type if isinstance(exc, VoiceFailure) else type(exc).__name__
                        )
                        self.attention = "MODEL_ERROR"
                        self.telemetry.count(
                            "model_failures_total", provider=self.providers.model.name
                        )
                        self.log("model_request_failed", error_type=error)
                        await self.store.failure(
                            self.trace_id,
                            VoiceFailure("MODEL_ERROR", "generate_response", error),
                            provider=self.providers.model.name,
                        )
                        # One deterministic authorized fallback; unchecked output never reaches TTS.
                        draft = ResponseDraft()
                        self.log("authorized_fallback_used", turn_index=turn)
                    await self.store.invocation(
                        trace_id=self.trace_id,
                        provider=self.providers.model.name,
                        model=self.providers.model.model,
                        prompt_version=version,
                        input_context_identifiers={
                            "business_id": business.id,
                            "rule_id": policy.rule_id,
                            "turn_index": turn,
                        },
                        output=output,
                        latency_ms=round((monotonic() - tick) * 1000),
                        token_usage=tokens,
                        error=error,
                        retry_count=0,
                    )
                    text = render_response(context, draft)
                    if self.settings.mode == "live":
                        text = render_hindi(context, draft)
                    self.log(
                        "model_request_completed",
                        prompt_version=version,
                        output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        fallback=error is not None,
                    )
                else:
                    text = fixed_text
                message_id = await self.store.assistant(text, turn)
                stage = "text_to_speech"
                self.attempts[generation] = (message_id, "GENERATED")
                tick = monotonic()
                resampler = PCMResampler(16000, self.protocol.start.sample_rate)
                buffer = PCMBuffer(3200)
                first = True
                with self.telemetry.stage("text_to_speech", self.call_id):
                    async with aclosing(self.providers.tts.stream(text, 16000)) as chunks:
                        while True:
                            try:
                                async with asyncio.timeout(self.settings.provider_timeout_s):
                                    chunk = await anext(chunks)
                            except StopAsyncIteration:
                                break
                            if (
                                not isinstance(chunk, bytes)
                                or len(chunk) % 2
                                or len(chunk) > 100_000
                            ):
                                raise VoiceFailure("TTS_ERROR", "invalid_pcm")
                            if not chunk:
                                continue
                            if first:
                                self.latency("tts_first_audio", tick, generation=generation)
                                first = False
                            for packet in buffer.feed(resampler.convert(chunk)):
                                await self.submit(
                                    AuthorizedPlayback(Packet(generation, packet), self.future())
                                )
                if first:
                    raise VoiceFailure("TTS_ERROR", "empty_audio")
                for packet in buffer.feed(resampler.convert(b"", final=True)):
                    await self.submit(AuthorizedPlayback(Packet(generation, packet), self.future()))
                last = buffer.finish(pad=True)
                if last:
                    await self.submit(AuthorizedPlayback(Packet(generation, last), self.future()))
                await self.submit(
                    AuthorizedPlayback(Packet(generation, message_id=message_id), self.future())
                )
        except asyncio.CancelledError:
            self.log("response_cancelled", generation=generation)
            raise
        except Exception as exc:
            self.fail(
                exc
                if isinstance(exc, VoiceFailure)
                else VoiceFailure(
                    "DATABASE_ERROR"
                    if isinstance(exc, SQLAlchemyError)
                    else "TTS_ERROR"
                    if stage == "text_to_speech"
                    else "CONTEXT_ERROR",
                    stage,
                    type(exc).__name__,
                )
            )

    async def read_speech(self):
        """Own the STT event reader and route each normalized event through Pipecat with
        acknowledgement. Unexpected stream completion or provider errors latch STT failure;
        cancellation is propagated for orderly session cleanup.
        """
        try:
            async for speech in self.providers.stt.events():
                await self.submit(RecognizedSpeech(speech, self.future()))
            if not self.stopping.is_set():
                self.fail(VoiceFailure("STT_ERROR", "stt_disconnected"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(
                exc
                if isinstance(exc, VoiceFailure)
                else VoiceFailure("STT_ERROR", "stt_events", type(exc).__name__)
            )

    async def receive(self):
        """Own Exotel ingress after the validated start event. Parse/validate each event;
        resample media to acknowledged Pipecat frames, persist playback ACKs, and stop on
        hangup. ACK state is published before database awaits to prevent interruption races.
        Disconnects retain attention; protocol and receive errors latch telephony failure.
        """
        try:
            while not self.stopping.is_set():
                message = self.protocol.parse(await self.socket.receive_text())
                if message["event"] == "media":
                    self.last_media = monotonic()
                    for pcm in self.frames.feed(self.resampler.convert(message["pcm"])):
                        await self.submit(
                            IncomingPCM(
                                audio=pcm, sample_rate=16000, num_channels=1, done=self.future()
                            )
                        )
                elif message["event"] == "mark":
                    message_id = self.playback.acknowledge(message["mark"]["name"])
                    if message_id:
                        # Publish the ACK before yielding to persistence: otherwise a
                        # concurrent speech_start can misclassify confirmed speech.
                        for gen, (mid, _) in self.attempts.items():
                            if mid == message_id:
                                self.attempts[gen] = (mid, "PLAYED")
                        await self.store.delivery(message_id, "PLAYED")
                        self.last_activity = monotonic()
                        self.log("playback_acknowledged")
                elif message["event"] == "dtmf":
                    self.log("dtmf_received")
                elif message["event"] == "stop":
                    self.playback.closed = True
                    self.reason = "CALL_ENDED"
                    self.stopping.set()
        except WebSocketDisconnect:
            self.reason, self.attention = "DISCONNECTED", "DISCONNECTED"
            self.stopping.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(
                exc
                if isinstance(exc, VoiceFailure)
                else VoiceFailure("TELEPHONY_ERROR", "receive", type(exc).__name__)
            )

    async def watchdog(self):
        """Check independent media, call, utterance, final-transcript, playback, and silence
        deadlines. When truly idle, queue one reviewed recovery prompt and later end with
        attention. Provider processing and pending playback suppress idle prompts; run()
        cancels this loop during cleanup.
        """
        while not self.stopping.is_set():
            await asyncio.sleep(0.02)
            now = monotonic()
            if now - self.last_media > self.settings.media_timeout_s:
                self.fail(VoiceFailure("TELEPHONY_ERROR", "media_timeout", "TimeoutError"))
            elif now - self.started > self.settings.max_call_s:
                self.fail(VoiceFailure("TELEPHONY_ERROR", "call_duration", "TimeoutError"))
            elif self.speech_since and now - self.speech_since > self.settings.max_utterance_s:
                self.fail(VoiceFailure("STT_ERROR", "utterance_timeout", "TimeoutError"))
            elif (
                self.awaiting_final and now - self.awaiting_final > self.settings.provider_timeout_s
            ):
                self.fail(VoiceFailure("STT_ERROR", "final_timeout", "TimeoutError"))
            elif any(
                now - stamp > self.settings.playback_timeout_s
                for _, stamp in self.playback.pending.values()
            ):
                self.fail(VoiceFailure("TELEPHONY_ERROR", "playback_timeout", "TimeoutError"))
            elif (
                not self.in_speech
                and not self.awaiting_final
                and not self.playback.pending
                and (self.response_task is None or self.response_task.done())
            ):
                idle = now - self.last_activity
                if idle > self.settings.silence_close_s:
                    self.reason, self.attention = "SILENCE_TIMEOUT", "SILENCE_TIMEOUT"
                    self.stopping.set()
                elif idle > self.settings.silence_prompt_s and not self.prompted:
                    self.prompted = True
                    self.generation += 1
                    self.playback.generation = self.generation
                    self.log("silence_recovery_prompt")
                    self.response_task = asyncio.create_task(
                        self.respond(0, self.generation, fixed_text=SILENCE_PROMPT)
                    )

    async def run(self):
        """Start the durable call, Pipecat, STT, concurrent loops, and greeting, then await
        termination. Own and join all spawned tasks and close providers/pipeline/socket on
        every path. Persist partials, sanitized failure diagnostics, delivery states, and
        aggregate outcome when call creation succeeded. Server cancellation is re-raised
        after cleanup; storage failure is logged by type because persistence cannot be
        guaranteed then. Return the application call ID.
        """
        tasks = []
        started = False
        with self.telemetry.stage("incoming_call", self.call_id) as root:
            self.trace_id = f"{root.get_span_context().trace_id:032x}"
            self.log("call_started", simulation=self.settings.mode == "mock")
            self.telemetry.count("calls_started_total")
            try:
                business = await self.store.start(
                    self.settings.business_id,
                    self.protocol.start,
                    self.trace_id,
                    self.settings.mode,
                    response_version=getattr(
                        self.providers.model, "prompt_version", RESPONSE_VERSION
                    ),
                )
                started = True
                await self.pipeline.start()
                await self.checked("stt_connect", self.providers.stt.start(), "STT_ERROR")
                tasks = [
                    asyncio.create_task(coro)
                    for coro in (
                        self.playback.run(),
                        self.read_speech(),
                        self.receive(),
                        self.watchdog(),
                    )
                ]
                self.response_task = asyncio.create_task(
                    self.respond(
                        0,
                        0,
                        fixed_text=(
                            GREETING_HI if self.settings.mode == "live" else GREETING
                        ).format(name=business.name),
                    )
                )
                await self.stopping.wait()
            except asyncio.CancelledError:
                self.fail(VoiceFailure("TELEPHONY_ERROR", "server_shutdown", "CancelledError"))
                raise
            except Exception as exc:
                self.fail(
                    exc
                    if isinstance(exc, VoiceFailure)
                    else VoiceFailure(
                        "DATABASE_ERROR" if isinstance(exc, SQLAlchemyError) else "CONTEXT_ERROR",
                        "session_start",
                        type(exc).__name__,
                    )
                )
            finally:
                self.stopping.set()
                await self.playback.close()
                if self.response_task:
                    self.response_task.cancel()
                    await asyncio.gather(self.response_task, return_exceptions=True)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for provider in (self.providers.stt, self.providers.model, self.providers.tts):
                    try:
                        await asyncio.wait_for(provider.close(), self.settings.provider_timeout_s)
                    except Exception as exc:
                        self.log("provider_cleanup_failed", error_type=type(exc).__name__)
                if hasattr(self.pipeline, "task"):
                    try:
                        await self.pipeline.close()
                    except Exception as exc:
                        self.fail(
                            VoiceFailure("UNKNOWN_ERROR", "pipeline_cleanup", type(exc).__name__)
                        )
                if self.failure:
                    root.set_status(Status(StatusCode.ERROR))
                    self.telemetry.count(
                        "stage_errors_total",
                        stage=self.failure.stage,
                        category=self.failure.category,
                    )
                    if self.failure.category == "DATABASE_ERROR":
                        self.telemetry.count("database_errors_total")
                    if not started:
                        self.log(
                            "stage_failed",
                            category=self.failure.category,
                            stage=self.failure.stage,
                            error_type=self.failure.error_type,
                        )
                        self.log("call_failed", persisted=False)
                        self.telemetry.count("calls_failed_total")
                if started:
                    try:
                        if self.partial:
                            await self.store.customer(self.partial, self.turn + 1, partial=True)
                            self.attention = self.attention or "PARTIAL_TRANSCRIPT"
                        if self.failure:
                            root.set_status(Status(StatusCode.ERROR))
                            await self.store.failure(
                                self.trace_id,
                                self.failure,
                                provider={
                                    "STT_ERROR": self.providers.stt.name,
                                    "TTS_ERROR": self.providers.tts.name,
                                    "MODEL_ERROR": self.providers.model.name,
                                }.get(self.failure.category),
                            )
                            self.log(
                                "stage_failed",
                                category=self.failure.category,
                                stage=self.failure.stage,
                                error_type=self.failure.error_type,
                            )
                        self.outcome = await self.store.finish(
                            self.reason, self.attention, failed=self.failure is not None
                        )
                        self.log("summary_generated", prompt_version="voice_call_summary:v1")
                        self.log(
                            "call_failed" if self.failure else "call_completed",
                            outcome=self.outcome,
                            reason=self.reason,
                        )
                        self.telemetry.count(
                            "calls_failed_total" if self.failure else "calls_completed_total"
                        )
                        self.telemetry.count(
                            "needs_attention_calls"
                            if self.outcome == "NEEDS_ATTENTION"
                            else "resolved_calls"
                        )
                    except Exception as exc:
                        self.log("failure_persistence_failed", error_type=type(exc).__name__)
                try:
                    await self.socket.close(code=1011 if self.failure else 1000)
                except (RuntimeError, OSError):
                    pass
        return self.call_id
