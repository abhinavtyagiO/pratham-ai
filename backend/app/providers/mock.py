import re

from app.domain.conversations import ModelContext, ResponseDraft, SummaryContext, SummaryDraft
from app.domain.types import Intent, IntentResult
from app.prompts.templates import INTENT_VERSION
from app.providers.interfaces import ModelResult, Providers


class MockTelephonyProvider:
    name = "mock_telephony"

    def identify_caller(self, caller_phone: str | None) -> str | None:
        # No contact recognition in slice 1. Caller ID is metadata, never authorization.
        return caller_phone


class MockSpeechToTextProvider:
    """Simulation transport: UTF-8 text bytes, explicitly not a speech recognizer."""

    name = "mock_stt"

    def transcribe(self, audio: bytes) -> str:
        text = audio.decode("utf-8").strip()
        if not text:
            raise ValueError("Empty simulated utterance")
        return text


class MockTextToSpeechProvider:
    """Simulation transport: UTF-8 bytes, explicitly not playable audio."""

    name = "mock_tts"

    def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")


class DeterministicIntentDetector:
    name = "deterministic_intent"
    version = INTENT_VERSION
    _price = re.compile(
        r"\b(?:rate|price|pricing|cost|daam|dam|keemat|kimat)\b"
        r"|\bkitne\s+(?:ka|ki|ke|mein|me)\b"
        r"|\bkitna\s+(?:padega|hoga|lagega|hai)\b"
        r"|\bhow\s+much\b|दाम|कीमत|रेट|प्राइस|कितने\s+का|कितना\s+(?:है|पड़ेगा)",
        re.IGNORECASE,
    )

    def detect(self, text: str) -> IntentResult:
        intent = Intent.PRICE_ENQUIRY if self._price.search(text) else Intent.UNKNOWN
        # No fabricated confidence score for a keyword classifier.
        return IntentResult(intent)


class MockConversationModelProvider:
    name = "mock"
    model = "deterministic-v1"

    def generate(
        self, context: ModelContext, *, prompt: str, version: str
    ) -> ModelResult[ResponseDraft]:
        return ModelResult(ResponseDraft())

    def summarize(
        self, context: SummaryContext, *, prompt: str, version: str
    ) -> ModelResult[SummaryDraft]:
        return ModelResult(SummaryDraft())


def build_providers(mode: str = "mock") -> Providers:
    if mode != "mock":
        raise ValueError("Only PROVIDER_MODE=mock is implemented in slice 1")
    return Providers(
        MockTelephonyProvider(),
        MockSpeechToTextProvider(),
        MockTextToSpeechProvider(),
        DeterministicIntentDetector(),
        MockConversationModelProvider(),
    )
