import asyncio
import base64
import json
from uuid import uuid4

from app.persistence.repository import CallRepository
from app.realtime.audio import tone
from app.realtime.config import VoiceSettings
from app.realtime.mock import mock_voice_providers
from app.realtime.protocol import ExotelProtocol
from app.realtime.session import RealtimeSession


class MemorySocket:
    """The exact Exotel JSON boundary with a bounded, in-process WebSocket peer."""

    def __init__(self):
        self.incoming = asyncio.Queue(maxsize=64)
        self.outgoing = asyncio.Queue(maxsize=64)
        self.sent = []
        self.closed = False

    async def receive_text(self):
        return await self.incoming.get()

    async def send_json(self, message):
        if self.closed:
            raise RuntimeError("Socket closed")
        self.sent.append(message)
        await self.outgoing.put(message)

    async def close(self, code=1000):
        self.closed = True


def start_event(*, call_sid="CA-demo", stream_sid="MZ-demo", sample_rate=8000):
    return {
        "event": "start",
        "sequence_number": "1",
        "stream_sid": stream_sid,
        "start": {
            "stream_sid": stream_sid,
            "call_sid": call_sid,
            "account_sid": "AC-demo",
            "from": "+919999999999",
            "to": "+918000000000",
            "media_format": {
                "encoding": "audio/x-raw",
                "sample_rate": str(sample_rate),
                "bit_rate": "16",
            },
        },
    }


class AudioHarness:
    def __init__(
        self,
        sessions,
        telemetry,
        *,
        settings=None,
        providers=None,
        utterances=None,
        sample_rate=8000,
    ):
        self.sessions, self.telemetry = sessions, telemetry
        self.settings = settings or VoiceSettings(pace_audio=False)
        self.providers = providers or mock_voice_providers(utterances)
        self.socket = MemorySocket()
        self.protocol = ExotelProtocol()
        self.start = start_event(
            call_sid=f"CA-{uuid4().hex}", stream_sid=f"MZ-{uuid4().hex}", sample_rate=sample_rate
        )
        self.protocol.parse(json.dumps(self.start))
        self.sequence, self.timestamp = 1, 0
        self.sample_rate = sample_rate

    async def begin(self):
        self.session = RealtimeSession(
            self.socket, self.protocol, self.settings, self.providers, self.sessions, self.telemetry
        )
        self.task = asyncio.create_task(self.session.run())
        await self.reply()  # greeting played/acknowledged before a synthetic caller starts
        return self

    async def feed(self, kind, **fields):
        self.sequence += 1
        await self.socket.incoming.put(
            json.dumps(
                {
                    "event": kind,
                    "sequence_number": str(self.sequence),
                    "stream_sid": self.start["stream_sid"],
                    **fields,
                }
            )
        )

    async def audio(self, pcm):
        chunk_size = self.sample_rate // 5  # 100 ms of PCM16
        for offset in range(0, len(pcm), chunk_size):
            data = pcm[offset : offset + chunk_size]
            await self.feed(
                "media",
                media={
                    "chunk": str(self.sequence),
                    "timestamp": str(round(self.timestamp)),
                    "payload": base64.b64encode(data).decode("ascii"),
                },
            )
            self.timestamp += len(data) * 1000 / (self.sample_rate * 2)

    async def utterance(self):
        await self.audio(
            tone(0.4, self.sample_rate)
            + b"\0" * round(self.sample_rate * 2 * (self.settings.silence_ms / 1000 + 0.1))
        )

    async def next(self, kind):
        async with asyncio.timeout(5):
            while True:
                result = await self.socket.outgoing.get()
                if result["event"] == kind:
                    return result

    async def reply(self):
        mark = await self.next("mark")
        await self.feed("mark", mark=mark["mark"])
        # Wait for durable delivery state, not merely for the client's ACK to be queued.
        async with asyncio.timeout(5):
            while mark["mark"]["name"] in self.session.playback.pending:
                await asyncio.sleep(0.001)
        return mark

    async def finish(self):
        if not self.task.done():
            await self.feed(
                "stop", stop={"call_sid": self.start["start"]["call_sid"], "reason": "callended"}
            )
        if not self.task.cancelled():
            await asyncio.wait_for(self.task, 8)
        with self.sessions() as db:
            return CallRepository(db).detail(self.session.call_id)
