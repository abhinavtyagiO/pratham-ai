"""Provider-neutral live voice contracts and safe failure/event envelopes."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.conversations import ModelContext, ResponseDraft
from app.providers.interfaces import ModelResult


@dataclass(frozen=True)
class SpeechEvent:
    """Immutable normalized STT/VAD event consumed by the session's speech state machine.
    Partial text cannot authorize a response; a final may carry a provider utterance ID for
    per-call deduplication.
    """

    kind: Literal["speech_start", "speech_end", "partial", "final"]
    text: str = ""
    utterance_id: str | None = None


class StreamingSTT(Protocol):
    """Per-call async recognizer contract separating socket startup, PCM input, boundaries, and
    event output. The session owns one reader and deadlines; implementations must release
    their own socket/keepalive on close and propagate cancellation.
    """

    name: str

    async def start(self):
        """Acquire the provider session and complete its handshake before audio/event
        processing. Startup failures propagate to session cleanup.
        """
        ...

    async def audio(self, pcm16: bytes):
        """Send raw 16 kHz mono PCM16 input asynchronously; caller supplies a stall deadline
        and may cancel on teardown.
        """
        ...

    async def boundary(self, kind: str):
        """Accept a local speech_start/end in manual mode. Server-VAD implementations must
        avoid sending conflicting manual boundaries.
        """
        ...

    def events(self) -> AsyncIterator[SpeechEvent]:
        """Return an async iterator of normalized VAD and transcript events. Unexpected
        completion is treated as provider disconnection by the owning session.
        """
        ...

    async def close(self):
        """Release recognizer-owned resources after the session cancels its reader. The owning
        session supplies a finite cleanup deadline.
        """
        ...


class AsyncResponseModel(Protocol):
    """Per-call asynchronous phrasing model accepting only authorized ModelContext. Return a
    complete ResponseDraft inside ModelResult; callers may cancel obsolete generation and
    must close provider resources afterward.
    """

    name: str
    model: str

    async def generate(self, context: ModelContext) -> ModelResult[ResponseDraft]:
        """Return a complete schema-limited response choice and optional usage for authorized
        context. Partial or arbitrary text is not a valid return; failures/cancellation
        propagate to the session's fallback or interruption handling.
        """
        ...

    async def close(self):
        """Release model-owned network clients after outstanding generation has been
        cancelled/joined.
        """
        ...


class StreamingTTS(Protocol):
    """Utterance-scoped speech synthesis contract for already-rendered authorized text.
    Iteration yields raw mono PCM16 at the requested rate; generator closure must release
    any active provider stream.
    """

    name: str

    def stream(self, text: str, sample_rate: int) -> AsyncIterator[bytes]:
        """Return an async iterator of whole PCM16 samples for authorized text.
        Cancelled/closed iteration must discard obsolete speech and close the utterance's
        resources.
        """
        ...

    async def close(self):
        """Release provider-wide resources after active stream generators have been closed by
        the session.
        """
        ...


@dataclass
class VoiceProviders:
    """Dependency bundle of fresh STT, response model, and TTS adapters for one call. The
    session owns their lifecycle; factories must not share mutable speech/socket state
    between concurrent calls.
    """

    stt: StreamingSTT
    model: AsyncResponseModel
    tts: StreamingTTS


class VoiceFailure(Exception):
    """Sanitized live-voice failure carrying bounded category, stage, and exception type. The
    session uses these fields for diagnostics and conservative outcomes; raw provider error
    strings must never be passed here.
    """

    def __init__(self, category: str, stage: str, error_type: str = "VoiceFailure"):
        """Store safe failure codes and expose only category as the exception message. This
        avoids accidental token or transcript disclosure when infrastructure renders an
        exception.
        """
        self.category, self.stage, self.error_type = category, stage, error_type
        super().__init__(category)
