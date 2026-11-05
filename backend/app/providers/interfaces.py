"""Synchronous Slice 1 provider contracts; live streaming interfaces are in realtime.contracts."""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from app.domain.conversations import ModelContext, ResponseDraft, SummaryContext, SummaryDraft
from app.domain.types import IntentResult

T = TypeVar("T")


@dataclass(frozen=True)
class ModelResult(Generic[T]):
    """Immutable generic model value plus optional actual input/output token counts. Models
    return constrained schema objects; missing usage stays None and is never estimated as
    provider-reported data.
    """

    value: T
    input_tokens: int | None = None
    output_tokens: int | None = None


class TelephonyProvider(Protocol):
    """Synchronous caller-metadata contract for the legacy simulation path. It does not
    authorize a caller or own a live Exotel session; the real-time router handles ingress
    independently.
    """

    name: str

    def identify_caller(self, caller_phone: str | None) -> str | None:
        """Return provider caller metadata for the supplied optional phone identifier.
        Recognition is metadata only and must not grant authorization.
        """
        ...


class SpeechToTextProvider(Protocol):
    """Legacy synchronous transcription boundary used by the single-turn orchestrator. The mock
    accepts UTF-8 simulation bytes; live PCM streaming uses realtime.contracts.StreamingSTT
    instead.
    """

    name: str

    def transcribe(self, audio: bytes) -> str:
        """Return a final transcript from the simulation audio payload or raise on
        invalid/empty input. This synchronous interface does not expose partials or turn
        events.
        """
        ...


class TextToSpeechProvider(Protocol):
    """Legacy synchronous synthesis boundary for simulation verification. Live playable audio
    uses StreamingTTS, whose output includes rate/framing invariants and cancellation
    ownership.
    """

    name: str

    def synthesize(self, text: str) -> bytes:
        """Return nonempty simulation output bytes for authorized rendered text. The
        orchestrator validates byte shape; the mock's UTF-8 transport is not playable
        speech.
        """
        ...


class IntentDetector(Protocol):
    """Deterministic classification contract exposing an auditable classifier version. It
    classifies final text before policy evaluation and cannot authorize speech on its own.
    """

    name: str
    version: str

    def detect(self, text: str) -> IntentResult:
        """Return one supported IntentResult for final text, keeping unsupported requests
        UNKNOWN. Confidence should be supplied only when actually measured.
        """
        ...


class ConversationModelProvider(Protocol):
    """Synchronous closed-schema response/summary model contract for Slice 1. Receives
    allowlisted contexts and explicit prompt versions, never raw caller instructions;
    streaming live models use AsyncResponseModel.
    """

    name: str
    model: str

    def generate(
        self, context: ModelContext, *, prompt: str, version: str
    ) -> ModelResult[ResponseDraft]:
        """Return a constrained ResponseDraft and optional usage for authorized context using
        the supplied prompt/version. Schema violations must propagate rather than becoming
        speech.
        """
        ...

    def summarize(
        self, context: SummaryContext, *, prompt: str, version: str
    ) -> ModelResult[SummaryDraft]:
        """Return a SummaryDraft acknowledgment for the authorized outcome context. The backend
        renders the actual summary, so this interface does not permit free-form customer
        claims.
        """
        ...


@dataclass
class Providers:
    """Dependency bundle for one synchronous orchestrator: telephony, STT, TTS, classifier, and
    constrained model. The default bundle is entirely deterministic and independent of the
    live voice adapters.
    """

    telephony: TelephonyProvider
    stt: SpeechToTextProvider
    tts: TextToSpeechProvider
    intent: IntentDetector
    model: ConversationModelProvider
