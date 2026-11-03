from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from app.domain.conversations import ModelContext, ResponseDraft, SummaryContext, SummaryDraft
from app.domain.types import IntentResult

T = TypeVar("T")


@dataclass(frozen=True)
class ModelResult(Generic[T]):
    value: T
    input_tokens: int | None = None
    output_tokens: int | None = None


class TelephonyProvider(Protocol):
    name: str

    def identify_caller(self, caller_phone: str | None) -> str | None: ...


class SpeechToTextProvider(Protocol):
    name: str

    def transcribe(self, audio: bytes) -> str: ...


class TextToSpeechProvider(Protocol):
    name: str

    def synthesize(self, text: str) -> bytes: ...


class IntentDetector(Protocol):
    name: str
    version: str

    def detect(self, text: str) -> IntentResult: ...


class ConversationModelProvider(Protocol):
    name: str
    model: str

    def generate(
        self, context: ModelContext, *, prompt: str, version: str
    ) -> ModelResult[ResponseDraft]: ...

    def summarize(
        self, context: SummaryContext, *, prompt: str, version: str
    ) -> ModelResult[SummaryDraft]: ...


@dataclass
class Providers:
    telephony: TelephonyProvider
    stt: SpeechToTextProvider
    tts: TextToSpeechProvider
    intent: IntentDetector
    model: ConversationModelProvider
