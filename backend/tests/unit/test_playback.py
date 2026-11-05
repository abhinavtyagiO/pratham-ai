import asyncio

import pytest

from app.realtime.config import VoiceSettings
from app.realtime.harness import MemorySocket
from app.realtime.playback import Packet, Playback
from app.realtime.router import SessionRegistry


async def test_bounded_playback_queue_fails_instead_of_growing():
    import json

    from app.realtime.contracts import VoiceFailure
    from app.realtime.harness import start_event
    from app.realtime.protocol import ExotelProtocol

    protocol = ExotelProtocol()
    protocol.parse(json.dumps(start_event()))
    settings = VoiceSettings(playback_queue_size=1, send_timeout_s=0.02)
    playback = Playback(MemorySocket(), protocol, settings, lambda g: None, lambda e: None)
    await playback.enqueue(Packet(0, b"\0" * 3200))
    with pytest.raises(VoiceFailure) as raised:
        await playback.enqueue(Packet(0, b"\0" * 3200))
    assert raised.value.category == "BACKPRESSURE_ERROR" and playback.queue.qsize() == 1


async def test_clear_drops_old_audio_and_late_acknowledgements():
    import json

    from app.realtime.harness import start_event
    from app.realtime.protocol import ExotelProtocol

    protocol = ExotelProtocol()
    protocol.parse(json.dumps(start_event()))
    socket = MemorySocket()
    playback = Playback(
        socket, protocol, VoiceSettings(pace_audio=False), lambda g: None, lambda e: None
    )
    await playback.enqueue(Packet(0, b"\0" * 3200))
    playback.pending["reply-0"] = ("old-message", 0)
    await playback.clear(1)
    assert playback.queue.empty() and playback.acknowledge("reply-0") is None
    await playback.enqueue(Packet(0, b"\0" * 3200))
    assert playback.queue.empty()
    assert socket.sent == [{"event": "clear", "stream_sid": "MZ-demo"}]


async def test_socket_send_timeout_and_registry_capacity():
    from app.realtime.contracts import VoiceFailure
    from app.realtime.protocol import ProtocolError

    class StalledSocket:
        """Socket double whose write never completes, forcing the production playback send
        deadline to report failure.
        """

        async def send_json(self, message):
            """Block without consuming a network resource until the caller's timeout cancels
            this synthetic write.
            """
            await asyncio.Event().wait()

    playback = Playback(
        StalledSocket(), None, VoiceSettings(send_timeout_s=0.02), lambda g: None, lambda e: None
    )
    with pytest.raises(VoiceFailure) as raised:
        await playback.send({"event": "clear"})
    assert raised.value.stage == "send_audio"
    registry = SessionRegistry(1)
    async with registry.reserve():
        with pytest.raises(ProtocolError):
            async with registry.reserve():
                pass
        await registry.claim("one")
        with pytest.raises(ProtocolError):
            await registry.claim("one")
    assert registry.connections == 0
