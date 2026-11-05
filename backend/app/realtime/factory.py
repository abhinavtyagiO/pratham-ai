from app.providers.deepseek import DeepSeekFlash
from app.providers.sarvam_realtime import SarvamRealtimeSTT, SarvamStreamingTTS
from app.realtime.contracts import VoiceProviders
from app.realtime.mock import mock_voice_providers


def validate_live_settings(settings):
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
    if settings.mode == "mock":
        if settings.vad_mode != "energy":
            raise ValueError("The audio mock requires energy VAD")
        return mock_voice_providers()
    validate_live_settings(settings)
    return VoiceProviders(
        SarvamRealtimeSTT(settings), DeepSeekFlash(settings), SarvamStreamingTTS(settings)
    )
