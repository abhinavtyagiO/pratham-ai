import math
from dataclasses import dataclass

import numpy as np
import soxr


class PCMResampler:
    """Stateful streaming resampling; resetting per packet creates clicks and timing drift."""

    def __init__(self, source_rate: int, target_rate: int):
        self.same_rate = source_rate == target_rate
        self.stream = (
            None
            if self.same_rate
            else soxr.ResampleStream(source_rate, target_rate, 1, dtype="int16", quality="LQ")
        )

    def convert(self, pcm: bytes, *, final=False) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM16 requires whole samples")
        if self.same_rate:
            return pcm
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16)
        return self.stream.resample_chunk(samples, last=final).astype("<i2").tobytes()


class PCMBuffer:
    def __init__(self, size: int):
        self.size = size
        self.pending = bytearray()

    def feed(self, audio: bytes) -> list[bytes]:
        self.pending.extend(audio)
        chunks = []
        while len(self.pending) >= self.size:
            chunks.append(bytes(self.pending[: self.size]))
            del self.pending[: self.size]
        return chunks

    def finish(self, *, pad=False) -> bytes:
        audio = bytes(self.pending)
        self.pending.clear()
        return audio.ljust(self.size, b"\0") if pad and audio else audio


def tone(duration_s=0.2, sample_rate=16000, frequency=440, amplitude=3000) -> bytes:
    """Real PCM test signal. It is audible, but deliberately does not simulate human speech."""
    t = np.arange(round(duration_s * sample_rate)) / sample_rate
    return (np.sin(2 * math.pi * frequency * t) * amplitude).astype("<i2").tobytes()


@dataclass
class EnergyVAD:
    threshold: int
    min_speech_ms: int
    silence_ms: int
    active: bool = False
    voiced_ms: int = 0
    quiet_ms: int = 0

    def update(self, pcm: bytes) -> str | None:
        # The runtime feeds exactly 20 ms / 640-byte frames at 16 kHz.
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
