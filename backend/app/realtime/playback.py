"""Bounded, generation-aware outbound Exotel playback and acknowledgment tracking."""

import asyncio
from dataclasses import dataclass
from time import monotonic

from app.realtime.contracts import VoiceFailure


@dataclass(frozen=True)
class Packet:
    """Immutable playback work tagged with the response generation that authorized it. Audio
    contains raw PCM; a message_id-only packet requests an end-of-reply mark. Both may be
    discarded after interruption without confirming delivery.
    """

    generation: int
    audio: bytes = b""
    message_id: str | None = None


class Playback:
    """Serialize bounded outbound media, clear, and mark events for one Exotel socket. Own a
    bounded FIFO, send/clear lock, active generation, and pending mark-to-message/timestamp
    map. The session owns the run task and persistence; callbacks report first-send timing
    and terminal failures. No state is shared across calls.
    """

    def __init__(self, socket, protocol, settings, on_sent, on_error):
        """Bind transport/settings/callbacks and allocate the bounded queue and send lock.
        on_sent receives a generation once; on_error receives a sanitized VoiceFailure. No
        task starts until the session invokes run().
        """
        self.socket, self.protocol, self.settings = socket, protocol, settings
        self.on_sent, self.on_error = on_sent, on_error
        self.queue = asyncio.Queue(maxsize=settings.playback_queue_size)
        self.lock = asyncio.Lock()
        self.generation = 0
        self.closed = False
        self.pending: dict[str, tuple[str, float]] = {}
        self.first_sent: set[int] = set()

    async def send(self, event):
        """Write one Exotel event under a send deadline. Convert socket stalls and transport
        failures to sanitized telephony errors; callers serialize writes with the playback
        lock where ordering matters.
        """
        try:
            async with asyncio.timeout(self.settings.send_timeout_s):
                await self.socket.send_json(event)
        except (TimeoutError, OSError, RuntimeError) as exc:
            raise VoiceFailure("TELEPHONY_ERROR", "send_audio", type(exc).__name__) from None

    async def enqueue(self, packet):
        """Queue a current-generation Packet with bounded backpressure. Closed or obsolete work
        is discarded; a saturated queue raises BACKPRESSURE_ERROR rather than growing memory
        or stalling ingress indefinitely.
        """
        if self.closed or packet.generation != self.generation:
            return
        try:
            async with asyncio.timeout(self.settings.send_timeout_s):
                await self.queue.put(packet)
        except TimeoutError:
            raise VoiceFailure("BACKPRESSURE_ERROR", "playback_queue", "TimeoutError") from None

    async def clear(self, generation):
        """Under the send lock, advance generation, forget pending marks, drain queued work,
        and send Exotel clear. This orders invalidation against in-flight sends; already
        playing carrier audio cannot be recalled. Send failures propagate to the caller.
        """
        async with self.lock:
            self.generation = generation
            self.pending.clear()
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            if not self.closed:
                await self.send(self.protocol.clear())

    async def run(self):
        """Drain outbound work, recheck generation under the send lock, and emit media then
        reply marks. Register marks before sending so immediate ACKs are recognized. Pace
        outside the lock to keep clear responsive. Join queue accounting on every path;
        propagate cancellation and report other failures through on_error.
        """
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
        """Remove a pending mark and return its transcript message ID, or None for
        unknown/cleared marks. This synchronous operation is the receipt boundary; the
        session owns the later durable PLAYED update.
        """
        entry = self.pending.pop(name, None)
        return entry[0] if entry else None

    async def close(self):
        """Disable new sends, invalidate generation, and forget marks under the send lock. The
        owning session cancels/joins the run task; closing playback does not close the
        WebSocket itself.
        """
        self.closed = True
        async with self.lock:
            self.generation += 1
            self.pending.clear()
