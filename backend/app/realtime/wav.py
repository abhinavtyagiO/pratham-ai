import struct

from app.realtime.contracts import VoiceFailure


class StreamingWAV:
    """Strip a validated streaming RIFF/WAVE header; never forward headers as PCM samples."""

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.header = bytearray()
        self.riff = False
        self.fmt = False
        self.in_data = False
        self.remaining = None
        self.carry = b""

    def feed(self, data: bytes) -> bytes:
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
        self.carry = data[-1:] if len(data) % 2 else b""
        return data[:-1] if self.carry else data

    def finish(self):
        if not self.in_data or self.carry or self.remaining not in (None, 0):
            raise VoiceFailure("TTS_ERROR", "wav_truncated")
