"""Credential-independent real-PCM simulation through the production call state machine."""

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
    """Bounded in-process Exotel peer used only by deterministic tests/simulation. Own
    independent incoming/outgoing queues and captured sent envelopes; no provider or phone
    connection exists. AudioHarness supplies the client side while RealtimeSession uses the
    WebSocket-like methods.
    """

    def __init__(self):
        """Allocate bounded event queues, sent history, and open state. Captured envelopes may
        contain synthetic audio, so this harness must not be attached to real customer
        traffic.
        """
        self.incoming = asyncio.Queue(maxsize=64)
        self.outgoing = asyncio.Queue(maxsize=64)
        self.sent = []
        self.closed = False

    async def receive_text(self):
        """Await the next client JSON event from the bounded incoming queue. Cancellation
        unblocks the session receiver during teardown.
        """
        return await self.incoming.get()

    async def send_json(self, message):
        """Capture a server envelope and await bounded outgoing capacity. Reject writes after
        close; consumers must drain events to model backpressure realistically.
        """
        if self.closed:
            raise RuntimeError("Socket closed")
        self.sent.append(message)
        await self.outgoing.put(message)

    async def close(self, code=1000):
        """Mark the fake transport closed without placing a phone call or altering queued
        evidence. The owning session cancels the pending receiver task separately.
        """
        self.closed = True


def start_event(*, call_sid="CA-demo", stream_sid="MZ-demo", sample_rate=8000):
    """Build a synthetic documented AgentStream start envelope for the supplied call/stream IDs
    and sample rate. Uses demo account/phone data only; the production parser still
    validates it before session creation.
    """
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
    """Drive the real PCM/Pipecat/session/store path with a bounded in-memory Exotel peer. Own
    unique synthetic stream identities, audio clock/sequence, and the session task. Defaults
    bypass .env and use scripted STT/tone TTS; explicitly injected adapters can exercise
    mocked wire protocols. Call finish() to join cleanup and retrieve durable evidence.
    """

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
        """Bind session factory/telemetry and optional settings/providers, validate a synthetic
        start, and initialize its media clock. No task starts here; implicit defaults are
        independent of developer credentials.
        """
        self.sessions, self.telemetry = sessions, telemetry
        self.settings = settings or VoiceSettings(_env_file=None, pace_audio=False)
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
        """Start RealtimeSession in an asyncio task and wait for the greeting's playback
        acknowledgment before returning this harness. Missing greeting/output fails under
        reply()'s deadline.
        """
        self.session = RealtimeSession(
            self.socket, self.protocol, self.settings, self.providers, self.sessions, self.telemetry
        )
        self.task = asyncio.create_task(self.session.run())
        await self.reply()  # greeting played/acknowledged before a synthetic caller starts
        return self

    async def feed(self, kind, **fields):
        """Queue a sequenced Exotel JSON event with the active stream ID and supplied fields.
        Await incoming capacity rather than discarding events under load.
        """
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
        """Split raw input PCM16 at the negotiated rate into 100 ms synthetic media envelopes.
        Advance timestamps by sample duration without real-time sleeping, keeping the
        deterministic scenario fast.
        """
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
        """Feed an audible synthetic tone followed by enough silence for local energy VAD to
        finish a turn. The scripted STT labels this signal; it does not recognize human
        speech.
        """
        await self.audio(
            tone(0.4, self.sample_rate)
            + b"\0" * round(self.sample_rate * 2 * (self.settings.silence_ms / 1000 + 0.1))
        )

    async def next(self, kind):
        """Drain server envelopes until the requested event kind appears, under a five-second
        deadline. Other events remain captured in socket.sent for later assertions.
        """
        async with asyncio.timeout(5):
            while True:
                result = await self.socket.outgoing.get()
                if result["event"] == kind:
                    return result

    async def reply(self):
        """Wait for a reply mark, echo its ACK, and wait for the ordered store transaction
        queue. This prevents the next synthetic turn from racing durable delivery updates;
        return the acknowledged mark envelope.
        """
        mark = await self.next("mark")
        await self.feed("mark", mark=mark["mark"])
        # Wait for durable delivery state, not merely for the client's ACK to be queued.
        async with asyncio.timeout(5):
            while mark["mark"]["name"] in self.session.playback.pending:
                await asyncio.sleep(0.001)
        # ACK receipt precedes the database write. Join the store's ordered transaction
        # queue before the harness permits another turn or inspects durable delivery.
        await self.session.store.execute(lambda db, repo: None)
        return mark

    async def finish(self):
        """Send a matching stop event if still running, join session cleanup, and return the
        persisted call detail. Cancelled session tasks are not re-awaited; the caller may
        use this helper after explicit cancellation/failure tests.
        """
        if not self.task.done():
            await self.feed(
                "stop", stop={"call_sid": self.start["start"]["call_sid"], "reason": "callended"}
            )
        if not self.task.cancelled():
            await asyncio.wait_for(self.task, 8)
        with self.sessions() as db:
            return CallRepository(db).detail(self.session.call_id)
