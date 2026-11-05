from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env", extra="ignore")
    enabled: bool = False
    mode: Literal["mock", "live"] = "mock"
    sarvam_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    stt_model: str = "saaras:v4"
    tts_model: str = "bulbul:v4-flash"
    llm_model: str = "deepseek-flash"
    language_code: str = "hi-IN"
    tts_speaker: str = "shubh_hi_customer"
    business_id: str = "00000000-0000-0000-0000-000000000001"
    exotel_ws_user: str = "pratham"
    exotel_ws_password: SecretStr | None = None
    exotel_account_sid: str | None = None
    exotel_to_number: str | None = None
    max_sessions: int = Field(default=8, ge=1, le=100)
    vad_mode: Literal["energy", "sarvam"] = "energy"
    vad_threshold: float = Field(default=0.3, ge=0.01, le=1)
    energy_rms: int = Field(default=500, ge=1, le=30000)
    min_speech_ms: int = Field(default=100, ge=20, le=2000)
    silence_ms: int = Field(default=700, ge=100, le=3000)
    max_utterance_s: float = Field(default=30, ge=1, le=120)
    handshake_timeout_s: float = Field(default=5, gt=0, le=30)
    provider_timeout_s: float = Field(default=8, gt=0, le=60)
    response_timeout_s: float = Field(default=30, gt=0, le=120)
    send_timeout_s: float = Field(default=2, gt=0, le=10)
    media_timeout_s: float = Field(default=20, gt=0, le=120)
    playback_timeout_s: float = Field(default=12, gt=0, le=60)
    silence_prompt_s: float = Field(default=10, gt=0, le=120)
    silence_close_s: float = Field(default=30, gt=0, le=300)
    max_call_s: float = Field(default=3600, gt=0, le=3600)
    playback_queue_size: int = Field(default=4, ge=1, le=16)
    pace_audio: bool = True

    @model_validator(mode="after")
    def valid_silence(self):
        if self.silence_close_s <= self.silence_prompt_s:
            raise ValueError("silence_close_s must exceed silence_prompt_s")
        return self
