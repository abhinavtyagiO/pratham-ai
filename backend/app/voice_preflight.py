import json
import sys

from app.config import Settings
from app.realtime.config import VoiceSettings


def main():
    voice, app = VoiceSettings(), Settings()
    fields = {
        "VOICE_SARVAM_API_KEY": voice.sarvam_api_key,
        "VOICE_DEEPSEEK_API_KEY": voice.deepseek_api_key,
        "VOICE_EXOTEL_WS_PASSWORD": voice.exotel_ws_password,
        "VOICE_EXOTEL_ACCOUNT_SID": voice.exotel_account_sid,
        "VOICE_EXOTEL_TO_NUMBER": voice.exotel_to_number,
        "API_KEY": app.api_key,
    }
    missing = [name for name, value in fields.items() if not value]
    print(
        json.dumps(
            {
                "voice_enabled": voice.enabled,
                "mode": voice.mode,
                "models": {"stt": voice.stt_model, "llm": voice.llm_model, "tts": voice.tts_model},
                "missing_environment_variables": missing,
                "network_calls_performed": False,
                "real_phone_acceptance": "NOT_TESTED",
            },
            indent=2,
        )
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
