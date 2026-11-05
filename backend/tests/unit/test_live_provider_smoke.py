"""Test opt-in, bounded failure reporting, and artifacts without any live requests."""

import asyncio
import json
import wave

import httpx
import pytest
from pydantic import SecretStr

from app import live_provider_smoke as smoke
from app.realtime.audio import tone
from app.realtime.config import VoiceSettings
from app.realtime.contracts import SpeechEvent


def test_explicit_opt_in_is_required_before_loading_settings(monkeypatch, capsys):
    def forbidden():
        raise AssertionError("Settings must not load without opt-in")

    monkeypatch.setattr(smoke, "VoiceSettings", forbidden)
    with pytest.raises(SystemExit) as error:
        smoke.main([])
    assert error.value.code == 2
    assert "--live" in capsys.readouterr().err


async def test_missing_credentials_never_construct_or_contact_adapters(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network adapter must not be constructed")

    monkeypatch.setattr(smoke, "SarvamStreamingTTS", forbidden)
    monkeypatch.setattr(smoke, "DeepSeekFlash", forbidden)
    report = await smoke.run_checks(VoiceSettings(_env_file=None), tmp_path, 1)
    assert not report["passed"] and report["execution"] == "LIVE"
    assert all(c["status"] == "BLOCKED" for c in report["checks"].values())
    assert not any(c["network_request_attempted"] for c in report["checks"].values())


class SyntheticTTS:
    """Yield real test PCM through the smoke utility's WAV and level validation."""

    def __init__(self, settings):
        """Accept production-style settings without retaining credentials or opening sockets."""
        pass

    async def stream(self, text, rate):
        """Require the fixed synthetic sentence and return audible mono PCM16."""
        assert text == smoke.SYNTHETIC_TEXT and rate == 16000
        yield tone(0.3)


class SyntheticSTT:
    """Model a normalized VAD/transcript stream while capturing resource cleanup."""

    def __init__(self, settings):
        """Require server VAD and isolate input/close state for this utility invocation."""
        assert settings.vad_mode == "sarvam"
        self.audio_bytes = 0
        self.closed = False

    async def start(self):
        """Complete the fake provider handshake without network activity."""
        pass

    async def audio(self, pcm):
        """Track how much synthetic speech/silence the paced sender submitted."""
        self.audio_bytes += len(pcm)

    async def events(self):
        """Yield a complete synthetic server-controlled turn, including a partial."""
        for kind in ("speech_start", "partial", "speech_end", "final"):
            yield SpeechEvent(kind, "synthetic text" if kind in ("partial", "final") else "")

    async def close(self):
        """Expose cleanup so timeout/cancellation tests can assert ownership."""
        self.closed = True


async def test_fake_checks_write_audible_wav_and_do_not_mask_independent_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(smoke, "SarvamStreamingTTS", SyntheticTTS)
    monkeypatch.setattr(smoke, "SarvamRealtimeSTT", SyntheticSTT)

    async def failing_model(settings):
        raise RuntimeError("PRIVATE_TOKEN_DO_NOT_LOG")

    monkeypatch.setattr(smoke, "check_model", failing_model)
    settings = VoiceSettings(
        _env_file=None, sarvam_api_key=SecretStr("key"), deepseek_api_key=SecretStr("key")
    )
    report = await smoke.run_checks(settings, tmp_path, 5)
    assert not report["passed"]
    assert report["checks"]["llm"]["status"] == "FAILED"
    assert report["checks"]["stt"]["status"] == report["checks"]["tts"]["status"] == "PASSED"
    assert report["checks"]["stt"]["diagnostics"]["event_counts"]["partial"] == 1
    assert "PRIVATE_TOKEN" not in json.dumps(report)
    with wave.open(str(tmp_path / "synthetic-speech.wav")) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.getnframes() == 4800


async def test_provider_deadline_is_reported_and_stt_dependency_is_blocked(tmp_path, monkeypatch):
    async def stalls(*args):
        await asyncio.Event().wait()

    async def valid_model(settings):
        return {"thinking": "disabled"}

    monkeypatch.setattr(smoke, "check_tts", stalls)
    monkeypatch.setattr(smoke, "check_model", valid_model)
    settings = VoiceSettings(
        _env_file=None, sarvam_api_key=SecretStr("key"), deepseek_api_key=SecretStr("key")
    )
    report = await smoke.run_checks(settings, tmp_path, 0.02)
    assert report["checks"]["tts"]["error_type"] == "TimeoutError"
    assert report["checks"]["stt"]["status"] == "BLOCKED"
    assert report["checks"]["llm"]["status"] == "PASSED"


def test_http_error_diagnostics_exclude_headers_body_url_and_secret():
    request = httpx.Request("POST", "https://example.test/PRIVATE_TOKEN")
    response = httpx.Response(401, request=request, text="PRIVATE_TOKEN")
    error = httpx.HTTPStatusError("PRIVATE_TOKEN", request=request, response=response)
    assert smoke.safe_error(error) == {"error_type": "HTTPStatusError", "http_status": 401}


async def test_stt_timeout_joins_sender_reader_and_closes_provider(monkeypatch):
    instances = []

    class NeverFinal(SyntheticSTT):
        """Keep the final pending so the outer deadline must own cancellation."""

        def __init__(self, settings):
            """Register this instance for a cleanup assertion after cancellation."""
            super().__init__(settings)
            instances.append(self)

        async def events(self):
            """Emit only a partial and block instead of pretending a final was recognized."""
            yield SpeechEvent("partial", "synthetic")
            await asyncio.Event().wait()

    monkeypatch.setattr(smoke, "SarvamRealtimeSTT", NeverFinal)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await smoke.check_stt(VoiceSettings(_env_file=None), tone(0.3))
    assert instances[0].closed
