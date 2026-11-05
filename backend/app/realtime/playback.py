import asyncio
from dataclasses import dataclass
from time import monotonic

from app.realtime.contracts import VoiceFailure


@dataclass(frozen=True)
class Packet:
    generation: int
    audio: bytes = b""
    message_id: str | None = None


class Playback:
    def __init__(self, socket, protocol, settings, on_sent, on_error):
        self.socket, self.protocol, self.settings = socket, protocol, settings
        self.on_sent, self.on_error = on_sent, on_error
        self.queue = asyncio.Queue(maxsize=settings.playback_queue_size)
        self.lock = asyncio.Lock()
        self.generation = 0
        self.closed = False
        self.pending: dict[str, tuple[str, float]] = {}
        self.first_sent: set[int] = set()

    async def send(self, event):
        try:
            async with asyncio.timeout(self.settings.send_timeout_s):
                await self.socket.send_json(event)
        except (TimeoutError, OSError, RuntimeError) as exc:
            raise VoiceFailure("TELEPHONY_ERROR", "send_audio", type(exc).__name__) from None

    async def enqueue(self, packet):
        if self.closed or packet.generation != self.generation:
            return
        try:
            async with asyncio.timeout(self.settings.send_timeout_s):
                await self.queue.put(packet)
        except TimeoutError:
            raise VoiceFailure("BACKPRESSURE_ERROR", "playback_queue", "TimeoutError") from None

    async def clear(self, generation):
        async with self.lock:
            self.generation = generation
            self.pending.clear()
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            if not self.closed:
                await self.send(self.protocol.clear())

    async def run(self):
        try:
            while True:
                packet = await self.queue.get()
                try:
                    async with self.lock:
                        if self.closed or packet.generation != self.generation:
                            continue
                        if packet.audio:
                            await self.send(self.protocol.media(packet.audio))
                            if packet.generation not in self.first_sent:
                                self.first_sent.add(packet.generation)
                                self.on_sent(packet.generation)
                        if packet.message_id:
                            name = f"reply-{packet.generation}"
                            # Register before sending; acknowledgements can arrive immediately.
                            self.pending[name] = (packet.message_id, monotonic())
                            await self.send(self.protocol.mark(name))
                    if packet.audio and self.settings.pace_audio:
                        await asyncio.sleep(
                            len(packet.audio) / (2 * self.protocol.start.sample_rate)
                        )
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_error(
                exc
                if isinstance(exc, VoiceFailure)
                else VoiceFailure("TELEPHONY_ERROR", "playback", type(exc).__name__)
            )

    def acknowledge(self, name):
        # A mark echoed after clear cannot confirm playback of the cancelled response.
        entry = self.pending.pop(name, None)
        return entry[0] if entry else None

    async def close(self):
        self.closed = True
        async with self.lock:
            self.generation += 1
            self.pending.clear()
