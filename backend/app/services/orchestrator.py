import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.domain.conversations import (
    ResponseDraft,
    SummaryContext,
    SummaryDraft,
    build_model_context,
    render_response,
    render_summary,
)
from app.domain.policies import PolicyEngine
from app.domain.types import Intent, Outcome, Speaker, classify_outcome
from app.observability.telemetry import Telemetry, event
from app.persistence.repository import CallRepository
from app.prompts.templates import (
    GREETING,
    GREETING_VERSION,
    RESPONSE_PROMPT,
    RESPONSE_VERSION,
    SUMMARY_PROMPT,
    SUMMARY_VERSION,
)
from app.providers.interfaces import Providers
from app.services.errors import BusinessNotFound, CallExecutionError, ErrorCategory


@dataclass(frozen=True)
class SimulationResult:
    call_id: str
    trace_id: str
    intent: Intent
    decision: str
    response: str
    outcome: Outcome


class ConversationOrchestrator:
    def __init__(self, repository: CallRepository, providers: Providers, telemetry: Telemetry):
        self.repo = repository
        self.providers = providers
        self.telemetry = telemetry
        self.policy = PolicyEngine()

    @contextmanager
    def _phase(self, name, category, provider=None, metric=None, events=None):
        self.stage, self.category, self.provider = name, category, provider
        with self.telemetry.stage(name, self.call_id, metric):
            start = perf_counter()
            if events:
                self._log(events[0], provider=provider)
            yield
            if events:
                self._log(
                    events[1],
                    provider=provider,
                    latency_ms=round((perf_counter() - start) * 1000, 3),
                )

    def _log(self, name, **fields):
        event(name, self.call_id, self.trace_id, **fields)

    def _message(self, speaker, text):
        with self._phase("persist_conversation", ErrorCategory.DATABASE_ERROR):
            self.repo.message(self.call_id, speaker, text)
            self._log("message_persisted", speaker=speaker, text_length=len(text))

    def _speak(self, text):
        with self._phase(
            "text_to_speech",
            ErrorCategory.TTS_ERROR,
            self.providers.tts.name,
            "tts_latency_ms",
            ("tts_started", "tts_completed"),
        ):
            audio = self.providers.tts.synthesize(text)
            if not isinstance(audio, bytes) or not audio:
                raise ValueError("Invalid synthesized audio")

    def _invoke_model(self, context, *, summary=False):
        model = self.providers.model
        version = SUMMARY_VERSION if summary else RESPONSE_VERSION
        output_type = SummaryDraft if summary else ResponseDraft
        started = perf_counter()
        self._log(
            "model_request_started", provider=model.name, model=model.model, prompt_version=version
        )
        output = None
        error = None
        tokens = {"input": None, "output": None}
        try:
            if summary:
                result = model.summarize(context, prompt=SUMMARY_PROMPT, version=version)
            else:
                result = model.generate(context, prompt=RESPONSE_PROMPT, version=version)
            value = output_type.model_validate(result.value)
            output = value.model_dump(mode="json")
            tokens = {"input": result.input_tokens, "output": result.output_tokens}
            self._log(
                "model_request_completed",
                provider=model.name,
                model=model.model,
                prompt_version=version,
                output_sha256=hashlib.sha256(value.model_dump_json().encode()).hexdigest(),
                latency_ms=round((perf_counter() - started) * 1000, 3),
                token_usage=tokens,
                retry_count=0,
            )
            for kind, count in tokens.items():
                if count is not None:
                    self.telemetry.counters["model_tokens_total"].add(
                        count, {"model": model.model, "kind": kind}
                    )
            return value
        except Exception as exc:
            error = type(exc).__name__
            self.telemetry.count("model_failures_total", provider=model.name, model=model.model)
            self._log(
                "model_request_failed",
                provider=model.name,
                model=model.model,
                prompt_version=version,
                error_type=error,
                retry_count=0,
            )
            raise
        finally:
            # Safe model diagnostics go to the app DB. Raw input/transcript is never logged.
            self.repo.invocation(
                call_id=self.call_id,
                trace_id=self.trace_id,
                provider=model.name,
                model=model.model,
                prompt_version=version,
                input_context_identifiers={
                    "business_id": self.business_id,
                    "rule_id": self.rule_id,
                    "schema": type(context).__name__,
                },
                output=output,
                latency_ms=round((perf_counter() - started) * 1000),
                token_usage=tokens,
                error=error,
                retry_count=0,
            )

    def run(self, business_id, utterance, caller_phone=None, *, replay_of=None, snapshot=None):
        self.call_id = str(uuid4())
        self.business_id = business_id
        self.rule_id = None
        self.stage, self.category, self.provider = (
            "incoming_call",
            ErrorCategory.UNKNOWN_ERROR,
            None,
        )
        started = perf_counter()
        with self.telemetry.stage("incoming_call", self.call_id) as root:
            self.trace_id = f"{root.get_span_context().trace_id:032x}"
            self.telemetry.count("calls_started_total")
            self._log(
                "call_started",
                business_id=business_id,
                simulation=True,
                replay_of_call_id=replay_of,
            )
            try:
                with self._phase("start_call", ErrorCategory.DATABASE_ERROR):
                    self.repo.start(
                        self.call_id, business_id, caller_phone, self.trace_id, replay_of
                    )
                with self._phase("load_business_context", ErrorCategory.CONTEXT_ERROR):
                    if snapshot:
                        business, rules = self.repo.restore_snapshot(snapshot)
                    else:
                        business, rules = self.repo.load_context(business_id)
                    self._log(
                        "business_context_loaded", business_id=business.id, rule_count=len(rules)
                    )
                with self._phase("persist_context_snapshot", ErrorCategory.DATABASE_ERROR):
                    self.repo.save_snapshot(
                        self.call_id,
                        self.repo.snapshot(
                            business,
                            rules,
                            {
                                "intent": self.providers.intent.version,
                                "policy": self.policy.version,
                                "response": RESPONSE_VERSION,
                                "summary": SUMMARY_VERSION,
                                "greeting": GREETING_VERSION,
                            },
                        ),
                    )
                with self._phase(
                    "identify_caller", ErrorCategory.TELEPHONY_ERROR, self.providers.telephony.name
                ):
                    self.providers.telephony.identify_caller(caller_phone)
                    self._log("caller_identified", known_customer=False)
                greeting = GREETING.format(name=business.name)
                self._speak(greeting)
                self._message(Speaker.ASSISTANT, greeting)
                with self._phase(
                    "speech_to_text",
                    ErrorCategory.STT_ERROR,
                    self.providers.stt.name,
                    "stt_latency_ms",
                    ("stt_started", "stt_completed"),
                ):
                    text = self.providers.stt.transcribe(utterance.encode("utf-8"))
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Empty transcription")
                self._message(Speaker.CUSTOMER, text)
                with self._phase(
                    "detect_intent",
                    ErrorCategory.INTENT_ERROR,
                    self.providers.intent.name,
                    "intent_latency_ms",
                ):
                    detected = self.providers.intent.detect(text)
                    if not isinstance(detected.intent, Intent):
                        raise ValueError("Invalid intent result")
                    self._log(
                        "intent_detected",
                        intent=detected.intent,
                        intent_confidence=detected.confidence,
                        classifier_version=self.providers.intent.version,
                    )
                    self.telemetry.count("intents_detected_total", intent=detected.intent)
                    if detected.intent == Intent.UNKNOWN:
                        self.telemetry.count("unknown_intent_total")
                with self._phase(
                    "evaluate_policy", ErrorCategory.POLICY_ERROR, metric="policy_latency_ms"
                ):
                    tick = perf_counter()
                    decision = self.policy.evaluate(business.id, detected.intent, rules)
                    self.rule_id = decision.rule_id
                    self._log(
                        "policy_evaluated",
                        intent=detected.intent,
                        rule_id=decision.rule_id,
                        decision=decision.decision,
                        latency_ms=round((perf_counter() - tick) * 1000, 3),
                    )
                    if decision.rule_id:
                        self.telemetry.count("rules_triggered_total", decision=decision.decision)
                    elif detected.intent == Intent.PRICE_ENQUIRY:
                        self._log("policy_missing", intent=detected.intent)
                with self._phase("persist_policy_decision", ErrorCategory.DATABASE_ERROR):
                    self.repo.decision(self.call_id, decision)
                with self._phase("build_model_context", ErrorCategory.CONTEXT_ERROR):
                    model_context = build_model_context(business, decision)
                with self._phase(
                    "generate_response",
                    ErrorCategory.MODEL_ERROR,
                    self.providers.model.name,
                    "llm_latency_ms",
                ):
                    draft = self._invoke_model(model_context)
                    response = render_response(model_context, draft)
                self._speak(response)
                self._message(Speaker.ASSISTANT, response)
                outcome = classify_outcome(decision)
                with self._phase(
                    "generate_call_summary",
                    ErrorCategory.MODEL_ERROR,
                    self.providers.model.name,
                    "llm_latency_ms",
                ):
                    context = SummaryContext(
                        intent=detected.intent, decision=decision.decision, outcome=outcome
                    )
                    summary_draft = self._invoke_model(context, summary=True)
                    summary = render_summary(context, summary_draft)
                    self._log("summary_generated", prompt_version=SUMMARY_VERSION)
                with self._phase("complete_call", ErrorCategory.DATABASE_ERROR):
                    self.repo.complete(self.call_id, outcome, summary, decision.attention_reason)
                if outcome == Outcome.NEEDS_ATTENTION:
                    self._log("call_marked_needs_attention", reason=decision.attention_reason)
                    self.telemetry.count("needs_attention_calls")
                else:
                    self.telemetry.count("resolved_calls")
                self.telemetry.count("calls_completed_total")
                self._log("call_completed", outcome=outcome)
                return SimulationResult(
                    self.call_id,
                    self.trace_id,
                    detected.intent,
                    decision.decision,
                    response,
                    outcome,
                )
            except BusinessNotFound:
                self._log("business_not_found", business_id=business_id)
                self.telemetry.count("calls_failed_total")
                raise
            except Exception as exc:
                category = (
                    ErrorCategory.DATABASE_ERROR
                    if isinstance(exc, SQLAlchemyError)
                    else self.category
                )
                retryable = isinstance(exc, (TimeoutError, ConnectionError))
                self.telemetry.count("calls_failed_total")
                self.telemetry.count("stage_errors_total", stage=self.stage, category=category)
                if category == ErrorCategory.DATABASE_ERROR:
                    self.telemetry.count("database_errors_total")
                failure_event = {
                    "speech_to_text": "stt_failed",
                    "detect_intent": "intent_detection_failed",
                    "text_to_speech": "tts_failed",
                }.get(self.stage, "stage_failed")
                self._log(
                    failure_event,
                    stage=self.stage,
                    category=category,
                    provider=self.provider,
                    error_type=type(exc).__name__,
                    retryable=retryable,
                )
                try:
                    persisted = self.repo.fail(
                        self.call_id,
                        self.trace_id,
                        self.stage,
                        category,
                        self.provider,
                        type(exc).__name__,
                        retryable,
                    )
                except Exception as storage_error:
                    self.telemetry.count("database_errors_total")
                    self._log(
                        "failure_persistence_failed",
                        category=ErrorCategory.DATABASE_ERROR,
                        error_type=type(storage_error).__name__,
                    )
                    persisted = False
                self._log("call_marked_needs_attention", reason=category, persisted=persisted)
                self.telemetry.count("needs_attention_calls")
                raise CallExecutionError(self.call_id, self.trace_id, category, self.stage) from exc
            finally:
                self.telemetry.histograms["end_to_end_turn_latency_ms"].record(
                    (perf_counter() - started) * 1000
                )
