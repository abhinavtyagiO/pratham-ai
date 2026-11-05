"""Exercise server VAD through the real adapter, Pipecat, playback, and durable store.

Only Sarvam's wire peer is scripted: PCM still crosses the Exotel boundary and
the real adapter translates the server events. These tests never contact Sarvam.
"""

import asyncio
import json

import pytest
from pydantic import SecretStr

from app.persistence.database import make_sessions
from app.providers.sarvam_realtime import SarvamRealtimeSTT
from app.realtime.audio import tone
from app.realtime.config import VoiceSettings
from app.realtime.harness import AudioHarness
from app.realtime.mock import mock_voice_providers


class ServerVADSocket:
    """Controllable Sarvam peer with an independent reader queue and captured writes."""

    def __init__(self):
        """Queue the provider handshake before the session starts reading events."""
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.emit("session.begin")

    def emit(self, kind, **fields):
        """Queue a server event without bypassing SarvamRealtimeSTT.events parsing."""
        self.incoming.put_nowait(json.dumps({"event": kind, **fields}))

    async def recv(self):
        """Block like a WebSocket until the next scripted server event arrives."""
        return await self.incoming.get()

    def __aiter__(self):
        """Allow the adapter to own its long-running provider receive loop."""
        return self

    async def __anext__(self):
        """Delegate asynchronous iteration to the same FIFO used by handshake reads."""
        return await self.recv()

    async def send(self, raw):
        """Capture client traffic so tests can reject accidental manual turn control."""
        self.sent.append(json.loads(raw))

    async def close(self):
        """Expose resource release after the owning call finishes."""
        self.closed = True


async def eventually(predicate):
    """Wait for a pipeline side effect with a deadline instead of relying on timing races."""
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("interrupt_at", ["model", "playback"])
async def test_server_vad_pauses_partials_barge_in_and_multiple_turns(
    engine, session, telemetry, interrupt_at
):
    settings = VoiceSettings(
        _env_file=None,
        vad_mode="sarvam",
        pace_audio=False,
        sarvam_api_key=SecretStr("synthetic-test-key"),
    )
    socket = ServerVADSocket()

    async def connector(url, **options):
        """Inject the fake wire peer while retaining the production STT adapter."""
        return socket

    providers = mock_voice_providers()
    providers.stt = SarvamRealtimeSTT(settings, connector=connector)
    generate = providers.model.generate
    entered, cancelled = asyncio.Event(), asyncio.Event()
    requests = []

    async def model(context):
        """Hold the first model request when testing cancellation before speech synthesis."""
        requests.append(context)
        if interrupt_at == "model" and len(requests) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await generate(context)

    providers.model.generate = model
    harness = await AudioHarness(
        make_sessions(engine), telemetry, settings=settings, providers=providers
    ).begin()
    try:
        socket.emit("vad.speech_start")
        socket.emit("transcript.partial", text="एमआरएफ")
        await eventually(lambda: harness.session.partial == "एमआरएफ")
        await harness.audio(tone(0.1) + b"\0" * 16000 + tone(0.1))
        # A pause longer than local energy VAD's threshold must not end a server-
        # controlled turn. Only the server's speech_end/final can authorize a reply.
        await eventually(lambda: len(socket.sent) >= 50)
        assert harness.session.in_speech and harness.session.turn == 0 and not requests
        socket.emit("transcript.partial", text="एमआरएफ का रेट")
        socket.emit("vad.speech_end")
        await eventually(lambda: harness.session.awaiting_final is not None)
        assert not harness.session.in_speech and not requests
        socket.emit("transcript.final", text="एमआरएफ का रेट क्या है?", utterance_id="one")
        if interrupt_at == "model":
            await asyncio.wait_for(entered.wait(), 3)
        else:
            old_mark = await harness.next("mark")
            assert old_mark["mark"]["name"] in harness.session.playback.pending

        cleared_before = sum(m["event"] == "clear" for m in harness.socket.sent)
        socket.emit("vad.speech_start")
        await eventually(
            lambda: sum(m["event"] == "clear" for m in harness.socket.sent) > cleared_before
        )
        if interrupt_at == "model":
            assert cancelled.is_set()
        else:
            # Exotel may echo a flushed mark after clear; it must not turn an
            # interrupted response into a confirmed PLAYED transcript entry.
            await harness.feed("mark", mark=old_mark["mark"])
        socket.emit("transcript.partial", text="फिर से")
        socket.emit("vad.speech_end")
        socket.emit("transcript.final", text="टायर की कीमत बताइए", utterance_id="two")
        await harness.reply()
        assert len(requests) == 2
        socket.emit("transcript.final", text="टायर की कीमत बताइए", utterance_id="two")
        socket.emit("vad.speech_start")
        socket.emit("transcript.partial", text="टायर")
        socket.emit("vad.speech_end")
        socket.emit("transcript.final", text="टायर का रेट क्या है?", utterance_id="three")
        await harness.reply()
        detail = await harness.finish()
    finally:
        if not harness.task.done():
            await harness.finish()

    assert socket.closed and not socket.incoming.qsize()
    assert {m["event"] for m in socket.sent} <= {"audio_input", "ping", "end"}
    assert len(requests) == 3
    assert len(detail["voice_turns"]) == 3
    assert [t["status"] for t in detail["voice_turns"]] == [
        "UNCONFIRMED" if interrupt_at == "model" else "INTERRUPTED",
        "PLAYED",
        "PLAYED",
    ]
    assert len(detail["policy_decisions"]) == 3
    assert all(p["decision"] == "DO_NOT_DISCLOSE_PRICE" for p in detail["policy_decisions"])
    assert len([m for m in detail["messages"] if m["speaker"] == "CUSTOMER"]) == 3
    assert detail["outcome"]["needs_attention"]
    if interrupt_at == "model":
        assert detail["model_invocations"][0]["error"] == "CancelledError"
