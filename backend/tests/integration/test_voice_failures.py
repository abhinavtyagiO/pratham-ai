import asyncio

import pytest

from app.persistence.database import make_sessions
from app.persistence.models import BusinessRule
from app.realtime.config import VoiceSettings
from app.realtime.harness import AudioHarness
from app.realtime.mock import mock_voice_providers
from app.realtime.session import RealtimeSession
from app.seed import RULE_ID


@pytest.mark.parametrize("kind", ["stt_connect", "tts_timeout", "stt_read"])
async def test_voice_provider_failures_are_durable_correlated_and_redacted(
    engine,
    session,
    telemetry,
    events,
    kind,
):
    settings = VoiceSettings(pace_audio=False, provider_timeout_s=0.05)
    providers = mock_voice_providers()

    async def fail():
        raise TimeoutError("PRIVATE_TOKEN_AND_CALLER")

    if kind == "stt_connect":
        providers.stt.start = fail
    elif kind == "tts_timeout":

        async def timeout(text, rate):
            await asyncio.Event().wait()
            yield b""

        providers.tts.stream = timeout
    else:

        async def broken_events():
            raise ConnectionError("PRIVATE_TOKEN_AND_CALLER")
            yield None

        providers.stt.events = broken_events
    harness = AudioHarness(make_sessions(engine), telemetry, settings=settings, providers=providers)
    harness.session = RealtimeSession(
        harness.socket, harness.protocol, settings, providers, harness.sessions, telemetry
    )
    harness.task = asyncio.create_task(harness.session.run())
    await asyncio.wait_for(harness.task, 3)
    detail = await harness.finish()
    assert detail["status"] == "FAILED" and detail["outcome"]["needs_attention"]
    assert detail["failures"][0]["category"] in ("STT_ERROR", "TTS_ERROR")
    assert detail["failures"][0]["provider"]
    assert all(e["call_id"] == detail["id"] for e in events)
    assert "PRIVATE_TOKEN_AND_CALLER" not in str(events)


async def test_rule_change_between_turns_fails_closed(engine, session, telemetry):
    harness = await AudioHarness(make_sessions(engine), telemetry).begin()
    await harness.utterance()
    await harness.reply()
    session.get(BusinessRule, RULE_ID).enabled = False
    session.commit()
    await harness.utterance()
    await harness.reply()
    detail = await harness.finish()
    assert detail["policy_decisions"][0]["decision"] == "DO_NOT_DISCLOSE_PRICE"
    assert detail["policy_decisions"][1]["decision"] == "REQUIRE_OWNER_ATTENTION"
    assert detail["outcome"]["needs_attention"]


async def test_server_shutdown_is_persisted_before_cancellation_propagates(
    engine, session, telemetry
):
    harness = await AudioHarness(make_sessions(engine), telemetry).begin()
    harness.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await harness.task
    detail = await harness.finish()
    assert detail["status"] == "FAILED"
    assert detail["failures"][0]["stage"] == "server_shutdown"


async def test_cancelled_model_request_has_diagnostic_and_never_reaches_tts(
    engine, session, telemetry
):
    providers = mock_voice_providers()
    entered = asyncio.Event()

    async def blocked(context):
        entered.set()
        await asyncio.Event().wait()

    providers.model.generate = blocked
    harness = await AudioHarness(make_sessions(engine), telemetry, providers=providers).begin()
    await harness.utterance()
    await asyncio.wait_for(entered.wait(), 3)
    detail = await harness.finish()
    assert detail["model_invocations"][0]["error"] == "CancelledError"
    assert len([m for m in detail["messages"] if m["speaker"] == "ASSISTANT"]) == 1
    assert detail["outcome"]["needs_attention"]
