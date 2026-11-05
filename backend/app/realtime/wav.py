"""Incremental, strict RIFF/WAVE-to-PCM decoding for streaming speech output."""

import struct

from app.realtime.contracts import VoiceFailure


class StreamingWAV:
    """Incrementally validate and strip Bulbul RIFF/WAVE framing into raw PCM16. Own fragmented
    header bytes, fmt/data state, remaining declared payload size, and a one-byte sample
    carry. One decoder belongs to one utterance; no headers or incomplete samples are
    emitted as playable audio.
    """

    def __init__(self, sample_rate):
        """Set the required sample rate and initialize empty header/chunk/sample state.
        Validation accepts only PCM codec 1, mono, 16-bit little-endian with two-byte block
        alignment.
        """
        self.sample_rate = sample_rate
        self.header = bytearray()
        self.riff = False
        self.fmt = False
        self.in_data = False
        self.remaining = None
        self.carry = b""

    def feed(self, data: bytes) -> bytes:
        """Consume a fragmented WAV byte stream and return only complete PCM samples. Validate
        RIFF/fmt metadata, skip bounded ancillary chunks with RIFF padding, and support
        unknown data length 0xffffffff. Retain partial header/sample bytes and reject
        incompatible or oversized headers with sanitized TTS failure.
        """
        if not self.in_data:
            self.header.extend(data)
            if not self.riff:
                if len(self.header) < 12:
                    return b""
                if self.header[:4] != b"RIFF" or self.header[8:12] != b"WAVE":
                    raise VoiceFailure("TTS_ERROR", "wav_header")
                del self.header[:12]
                self.riff = True
            while not self.in_data and len(self.header) >= 8:
                kind, size = struct.unpack("<4sI", self.header[:8])
                if kind == b"data":
                    if not self.fmt:
                        raise VoiceFailure("TTS_ERROR", "wav_format_missing")
                    self.in_data = True
                    # Streaming providers cannot always know total length up front.
                    # Accept RIFF's unknown-length sentinel without relaxing PCM checks.
                    self.remaining = None if size == 0xFFFFFFFF else size
                    data = bytes(self.header[8:])
                    self.header.clear()
                    break
                if size > 8192:
                    raise VoiceFailure("TTS_ERROR", "wav_header_size")
                end = 8 + size + size % 2
                if len(self.header) < end:
                    return b""
                if kind == b"fmt ":
                    if size < 16:
                        raise VoiceFailure("TTS_ERROR", "wav_format")
                    codec, channels, rate, _, align, bits = struct.unpack(
                        "<HHIIHH", self.header[8:24]
                    )
                    if (codec, channels, rate, align, bits) != (1, 1, self.sample_rate, 2, 16):
                        raise VoiceFailure("TTS_ERROR", "wav_format")
                    self.fmt = True
                del self.header[:end]
            if not self.in_data:
                if len(self.header) > 8192:
                    raise VoiceFailure("TTS_ERROR", "wav_header_size")
                return b""
        if self.remaining is not None:
            data = data[: self.remaining]
            self.remaining -= len(data)
        data = self.carry + data
        # Network boundaries may bisect a 16-bit sample. Hold its first byte until
        # the next fragment so downstream buffers always receive aligned PCM16.
        self.carry = data[-1:] if len(data) % 2 else b""
        return data[:-1] if self.carry else data

    def finish(self):
        """Require a parsed data chunk, complete final sample, and exhausted declared payload.
        Raise wav_truncated for incomplete output; unknown-length streaming data is allowed
        once its samples are aligned.
        """
        if not self.in_data or self.carry or self.remaining not in (None, 0):
            raise VoiceFailure("TTS_ERROR", "wav_truncated")
