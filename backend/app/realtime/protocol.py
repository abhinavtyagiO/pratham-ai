import base64
import binascii
import json
from dataclasses import dataclass


class ProtocolError(ValueError):
    """Only bounded, non-sensitive error codes cross this boundary."""


@dataclass(frozen=True)
class StartInfo:
    stream_sid: str
    call_sid: str
    account_sid: str
    caller_phone: str | None
    to_number: str | None
    sample_rate: int


class ExotelProtocol:
    """AgentStream Voicebot protocol, snake_case fields and PCM16LE, not Twilio mulaw."""

    def __init__(self):
        self.start: StartInfo | None = None
        self.stopped = False
        self.sequence = -1
        self.timestamp = -1

    def parse(self, raw: str) -> dict:
        if not isinstance(raw, str) or len(raw) > 150_000:
            raise ProtocolError("INVALID_MESSAGE_SIZE")
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError
            kind = message["event"]
            if self.stopped or kind not in {"connected", "start", "media", "mark", "dtmf", "stop"}:
                raise ValueError
            if "sequence_number" in message:
                seq = int(message["sequence_number"])
                if seq <= self.sequence:
                    raise ValueError
                self.sequence = seq
            if kind == "connected":
                if self.start:
                    raise ValueError
            elif kind == "start":
                if self.start:
                    raise ValueError
                data = message["start"]
                fmt = data["media_format"]
                rate = int(fmt["sample_rate"])
                if (
                    fmt["encoding"] != "audio/x-raw"
                    or int(fmt["bit_rate"]) != 16
                    or rate not in (8000, 16000, 24000)
                ):
                    raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT")
                ids = [data[k] for k in ("stream_sid", "call_sid", "account_sid")]
                if any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in ids):
                    raise ValueError
                if message.get("stream_sid", ids[0]) != ids[0]:
                    raise ValueError
                phone = data.get("from")
                if phone is not None and (not isinstance(phone, str) or len(phone) > 30):
                    raise ValueError
                self.start = StartInfo(*ids, phone, data.get("to"), rate)
            else:
                if self.start is None or message.get("stream_sid") != self.start.stream_sid:
                    raise ValueError
                if kind == "media":
                    media = message["media"]
                    pcm = base64.b64decode(media["payload"], validate=True)
                    if not pcm or len(pcm) > 100_000 or len(pcm) % 2:
                        raise ValueError
                    timestamp = int(media.get("timestamp", self.timestamp + 1))
                    if timestamp < self.timestamp:
                        raise ValueError
                    self.timestamp = timestamp
                    message["pcm"] = pcm
                elif kind == "mark":
                    name = message["mark"]["name"]
                    if not isinstance(name, str) or len(name) > 100:
                        raise ValueError
                elif kind == "dtmf":
                    if message["dtmf"]["digit"] not in "0123456789*#ABCD":
                        raise ValueError
                elif kind == "stop":
                    if message["stop"].get("call_sid") != self.start.call_sid:
                        raise ValueError
                    self.stopped = True
            return message
        except ProtocolError:
            raise
        except (KeyError, ValueError, TypeError, binascii.Error) as from_error:
            raise ProtocolError("INVALID_EXOTEL_EVENT") from from_error

    def media(self, pcm: bytes):
        if len(pcm) < 3200 or len(pcm) > 100_000 or len(pcm) % 320:
            raise ProtocolError("INVALID_OUTPUT_CHUNK")
        return {
            "event": "media",
            "stream_sid": self.start.stream_sid,
            "media": {"payload": base64.b64encode(pcm).decode("ascii")},
        }

    def mark(self, name: str):
        return {"event": "mark", "stream_sid": self.start.stream_sid, "mark": {"name": name}}

    def clear(self):
        return {"event": "clear", "stream_sid": self.start.stream_sid}
