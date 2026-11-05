"""PCM16 buffering/resampling and baseline energy VAD; synthetic tones are test data."""

import math
from dataclasses import dataclass

import numpy as np
import soxr


class PCMResampler:
    """Convert continuous mono PCM16 between rates without resetting filter history per packet.
    Own one soxr stream unless rates match. Each input stream/utterance needs its own
    instance; calls are synchronous and ordered by the owning session.
    """

    def __init__(self, source_rate: int, target_rate: int):
        """Allocate an int16 mono soxr stream for source_rate to target_rate, or a pass-through
        when equal. The instance owns filter delay/history until convert(final=True) flushes
        it.
        """
        self.same_rate = source_rate == target_rate
        self.stream = (
            None
            if self.same_rate
            else soxr.ResampleStream(source_rate, target_rate, 1, dtype="int16", quality="LQ")
        )

    def convert(self, pcm: bytes, *, final=False) -> bytes:
        """Return resampled little-endian PCM16 bytes, retaining filter state between calls.
        Reject odd byte counts. final=True flushes delayed samples at utterance end; do not
        reuse that soxr stream for a later utterance.
        """
        if len(pcm) % 2:
            raise ValueError("PCM16 requires whole samples")
        if self.same_rate:
            return pcm
        # soxr keeps filter history/delay across packets. Flushing every frame would
        # introduce discontinuities and change the caller's effective audio clock.
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16)
        return self.stream.resample_chunk(samples, last=final).astype("<i2").tobytes()


class PCMBuffer:
    """Accumulate arbitrary PCM fragments into fixed-size immutable packets. Own only the
    trailing bytearray; ingress uses 640-byte internal frames and playback uses 3200-byte
    Exotel packets. The owning session calls it synchronously.
    """

    def __init__(self, size: int):
        """Set the packet size in bytes and initialize empty trailing storage. Callers choose
        sizes aligned to PCM16 samples and their transport's framing contract.
        """
        self.size = size
        self.pending = bytearray()

    def feed(self, audio: bytes) -> list[bytes]:
        """Append audio bytes and return all complete packets in arrival order. Retain only the
        incomplete tail so network/provider fragmentation does not create partial audio
        frames.
        """
        self.pending.extend(audio)
        chunks = []
        while len(self.pending) >= self.size:
            chunks.append(bytes(self.pending[: self.size]))
            del self.pending[: self.size]
        return chunks

    def finish(self, *, pad=False) -> bytes:
        """Drain the incomplete tail and optionally zero-pad it to one packet. Padding is
        needed for Exotel's minimum output size; an empty buffer stays empty.
        """
        audio = bytes(self.pending)
        self.pending.clear()
        # Exotel requires a minimum-sized final packet; zero padding preserves speech
        # samples while satisfying framing instead of dropping the utterance tail.
        return audio.ljust(self.size, b"\0") if pad and audio else audio


def tone(duration_s=0.2, sample_rate=16000, frequency=440, amplitude=3000) -> bytes:
    """Real PCM test signal. It is audible, but deliberately does not simulate human speech."""
    t = np.arange(round(duration_s * sample_rate)) / sample_rate
    return (np.sin(2 * math.pi * frequency * t) * amplitude).astype("<i2").tobytes()


@dataclass
class EnergyVAD:
    """Baseline RMS turn detector for fixed 20 ms internal frames. Maintain active speech plus
    voiced/quiet duration counters; min_speech_ms rejects short noise and silence_ms
    tolerates pauses. This local mode drives manual STT boundaries and is bypassed in Sarvam
    VAD mode.
    """

    threshold: int
    min_speech_ms: int
    silence_ms: int
    active: bool = False
    voiced_ms: int = 0
    quiet_ms: int = 0

    def update(self, pcm: bytes) -> str | None:
        # The runtime feeds exactly 20 ms / 640-byte frames at 16 kHz.
        """Consume exactly one 640-byte PCM16 frame at 16 kHz and return speech_start,
        speech_end, or None. Voiced frames reset quiet duration; silence only ends an active
        turn after the configured threshold. The frame-size invariant is enforced by the
        session's PCMBuffer.
        """
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        voiced = bool(len(samples) and np.sqrt(np.mean(samples * samples)) >= self.threshold)
        if voiced:
            self.voiced_ms += 20
            self.quiet_ms = 0
            if not self.active and self.voiced_ms >= self.min_speech_ms:
                self.active = True
                return "speech_start"
        else:
            self.quiet_ms += 20
            if not self.active:
                self.voiced_ms = 0
            elif self.quiet_ms >= self.silence_ms:
                self.active = False
                self.voiced_ms = 0
                return "speech_end"
        return None
