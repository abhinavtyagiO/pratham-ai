"""Explicit provider selection and fail-closed live configuration validation."""

from app.providers.deepseek import DeepSeekFlash
from app.providers.sarvam_realtime import SarvamRealtimeSTT, SarvamStreamingTTS
from app.realtime.contracts import VoiceProviders
from app.realtime.mock import mock_voice_providers


def validate_live_settings(settings):
    """Fail closed when required provider or Exotel ingress credentials/account settings are
    missing. Raise ValueError containing field names only; this checks presence, not
    balance, model entitlement, network reachability, or authentication success.
    """
    missing = [
        name
        for name in (
            "sarvam_api_key",
            "deepseek_api_key",
            "exotel_ws_password",
            "exotel_account_sid",
            "exotel_to_number",
        )
        if not getattr(settings, name)
    ]
    if missing:
        raise ValueError("Live voice requires: " + ", ".join(missing))


def build_voice_providers(settings):
    """Build fresh per-call adapters for explicitly selected live or mock mode. Mock audio
    requires energy VAD; live mode validates required configuration and never silently
    substitutes mocks. The RealtimeSession owns and closes all returned adapters.
    """
    if settings.mode == "mock":
        if settings.vad_mode != "energy":
            raise ValueError("The audio mock requires energy VAD")
        return mock_voice_providers()
    validate_live_settings(settings)
    return VoiceProviders(
        SarvamRealtimeSTT(settings), DeepSeekFlash(settings), SarvamStreamingTTS(settings)
    )
