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
    def __init__(self, socket, protocol, settings, providers, sessions, telemetry):
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
        event(name, self.call_id, self.trace_id, **safe)

    def latency(self, name, since, **safe):
        elapsed = round((monotonic() - since) * 1000, 3)
        self.log(name, latency_ms=elapsed, **safe)
        self.telemetry.histograms["voice_stage_latency_ms"].record(elapsed, {"stage": name})

    def first_sent(self, generation):
        self.sent_at[generation] = monotonic()
        self.latency("first_audio_sent", self.response_started, generation=generation)

    def fail(self, failure):
        if self.failure is None:
            self.failure = failure
            self.attention = failure.category
            self.reason = "FAILED"
            self.stopping.set()

    async def checked(self, stage, coroutine, category):
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
        await self.pipeline.submit(frame, self.settings.provider_timeout_s)

    def future(self):
        return asyncio.get_running_loop().create_future()

    async def audio(self, pcm):
        if self.stopping.is_set():
            return
        if self.settings.vad_mode == "sarvam":
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
        tick = monotonic()
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
                await self.store.delivery(message_id, "INTERRUPTED")
                self.attempts[gen] = (message_id, "INTERRUPTED")
        else:
            self.playback.generation = self.generation
        if interrupted:
            self.latency("barge_in_cancelled", tick, generation=self.generation)

    async def speech(self, speech):
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
                        await self.store.delivery(message_id, "PLAYED")
                        for gen, (mid, _) in self.attempts.items():
                            if mid == message_id:
                                self.attempts[gen] = (mid, "PLAYED")
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
