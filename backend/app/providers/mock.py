"""Deterministic legacy simulation providers and the shared bounded intent detector."""

import re

from app.domain.conversations import ModelContext, ResponseDraft, SummaryContext, SummaryDraft
from app.domain.types import Intent, IntentResult
from app.prompts.templates import INTENT_VERSION
from app.providers.interfaces import ModelResult, Providers


class MockTelephonyProvider:
    """Stateless caller-metadata pass-through for deterministic simulation. It performs no
    contact lookup or network request and grants no authorization based on caller identity.
    """

    name = "mock_telephony"

    def identify_caller(self, caller_phone: str | None) -> str | None:
        # No contact recognition in slice 1. Caller ID is metadata, never authorization.
        """Return the supplied optional caller phone unchanged for simulation metadata. Caller
        ID is never an owner-policy or authentication capability.
        """
        return caller_phone


class MockSpeechToTextProvider:
    """Simulation transport: UTF-8 text bytes, explicitly not a speech recognizer."""

    name = "mock_stt"

    def transcribe(self, audio: bytes) -> str:
        """Decode and strip UTF-8 simulation bytes, returning nonempty text. Invalid encoding
        or empty utterances raise rather than pretending that a real audio recognizer
        succeeded.
        """
        text = audio.decode("utf-8").strip()
        if not text:
            raise ValueError("Empty simulated utterance")
        return text


class MockTextToSpeechProvider:
    """Simulation transport: UTF-8 bytes, explicitly not playable audio."""

    name = "mock_tts"

    def synthesize(self, text: str) -> bytes:
        """Encode authorized rendered text as UTF-8 simulation transport bytes. These bytes
        test the legacy boundary only and must never be sent to Exotel as playable PCM.
        """
        return text.encode("utf-8")


class DeterministicIntentDetector:
    """Stateless keyword classifier for supported English/Hinglish/Devanagari price enquiries.
    It deliberately supports only PRICE_ENQUIRY and UNKNOWN; it has no conversational
    pronoun resolution or general intent model.
    """

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
        """Match final text against reviewed price keywords and return an IntentResult. Leave
        confidence unset because regex matching does not provide a measured probability;
        classification alone never authorizes disclosure.
        """
        intent = Intent.PRICE_ENQUIRY if self._price.search(text) else Intent.UNKNOWN
        # No fabricated confidence score for a keyword classifier.
        return IntentResult(intent)


class MockConversationModelProvider:
    """Stateless deterministic choice/acknowledgment model for simulation and golden
    evaluations. Produces the same closed schemas required of real models without network
    calls or invented token usage.
    """

    name = "mock"
    model = "deterministic-v1"

    def generate(
        self, context: ModelContext, *, prompt: str, version: str
    ) -> ModelResult[ResponseDraft]:
        """Return the default reviewed ResponseDraft for interface-compatible authorized
        context. Prompt/version arguments preserve orchestration auditability without
        executing arbitrary text.
        """
        return ModelResult(ResponseDraft())

    def summarize(
        self, context: SummaryContext, *, prompt: str, version: str
    ) -> ModelResult[SummaryDraft]:
        """Return a summary acknowledgment, leaving all actual summary wording to the backend
        renderer.
        """
        return ModelResult(SummaryDraft())


def build_providers(mode: str = "mock") -> Providers:
    """Create the legacy deterministic provider bundle only when mode is mock. Reject
    unsupported modes instead of silently claiming a live integration; the streaming factory
    is separate.
    """
    if mode != "mock":
        raise ValueError("Only PROVIDER_MODE=mock is implemented in slice 1")
    return Providers(
        MockTelephonyProvider(),
        MockSpeechToTextProvider(),
        MockTextToSpeechProvider(),
        DeterministicIntentDetector(),
        MockConversationModelProvider(),
    )
