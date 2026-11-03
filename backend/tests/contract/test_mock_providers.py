import pytest

from app.domain.conversations import ModelContext, ResponseDraft, SummaryContext, SummaryDraft
from app.domain.types import Action, Intent
from app.prompts.templates import RESPONSE_PROMPT, RESPONSE_VERSION, SUMMARY_PROMPT, SUMMARY_VERSION
from app.providers.mock import build_providers


def test_simulation_telephony_stt_and_tts_contracts():
    providers = build_providers()
    assert providers.telephony.identify_caller(None) is None
    assert providers.telephony.identify_caller("+919999999999") == "+919999999999"
    text = "MRF ka rate क्या है?"
    assert providers.stt.transcribe(text.encode()) == text
    assert providers.tts.synthesize(text) == text.encode()
    assert providers.intent.detect(text).intent == Intent.PRICE_ENQUIRY
    assert providers.intent.detect("Namaste").intent == Intent.UNKNOWN


def test_model_contracts_are_versioned_structured_and_report_unknown_token_usage():
    model = build_providers().model
    response = model.generate(
        ModelContext(decision=Action.DO_NOT_DISCLOSE_PRICE, closing_time_phrase="shaam 6:30"),
        prompt=RESPONSE_PROMPT,
        version=RESPONSE_VERSION,
    )
    assert isinstance(response.value, ResponseDraft)
    assert response.input_tokens is None and response.output_tokens is None
    summary = model.summarize(
        SummaryContext(
            intent=Intent.PRICE_ENQUIRY, decision=Action.DO_NOT_DISCLOSE_PRICE, outcome="RESOLVED"
        ),
        prompt=SUMMARY_PROMPT,
        version=SUMMARY_VERSION,
    )
    assert isinstance(summary.value, SummaryDraft)


@pytest.mark.parametrize("audio", [b"", b"  ", b"\xff"])
def test_invalid_stt_input_raises_instead_of_fabricating(audio):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        build_providers().stt.transcribe(audio)


def test_real_provider_mode_is_never_silently_mocked():
    with pytest.raises(ValueError):
        build_providers("sarvam")
