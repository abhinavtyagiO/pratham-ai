"""Strict Exotel event validation and normalization of the two documented PCM metadata
dialects.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass


class ProtocolError(ValueError):
    """Bounded Exotel validation error safe to propagate across the ingress boundary. Messages
    are static codes rather than raw payloads; the router closes rejected sessions and live
    receive records only sanitized error types.
    """


@dataclass(frozen=True)
class StartInfo:
    """Immutable normalized metadata from one validated Exotel start event. The protocol owns
    the record; the router checks account/destination before constructing providers, and the
    session uses sample_rate to configure PCM conversion.
    """

    stream_sid: str
    call_sid: str
    account_sid: str
    caller_phone: str | None
    to_number: str | None
    sample_rate: int


def media_sample_rate(media_format: dict) -> int:
    """Normalize Exotel's two documented PCM16 descriptions, rejecting ambiguity.

    AgentStream uses ``audio/x-raw`` with ``bit_rate=16`` as sample depth.
    The Voicebot applet uses ``raw`` with ``bit_rate`` in kilobits/second:
    mono PCM16 therefore requires ``sample_rate * 16 / 1000``. Only these
    encoding/unit pairs and 8/16/24 kHz are accepted. No codec conversion or
    state mutation occurs; malformed or unsupported metadata raises ProtocolError.
    """

    def integer(value):
        """Accept integer metadata and decimal strings without truncating floats or booleans."""
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value):
            return int(value)
        raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT")

    if not isinstance(media_format, dict):
        raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT")
    try:
        rate = integer(media_format["sample_rate"])
        bits = integer(media_format["bit_rate"])
        encoding = media_format["encoding"]
        channels = integer(media_format.get("channels", 1))
    except KeyError:
        raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT") from None
    # The units follow the encoding spelling; accepting either number for either
    # spelling would conceal an unsupported format behind contradictory metadata.
    if rate not in (8000, 16000, 24000) or channels != 1:
        raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT")
    if not (
        (encoding == "audio/x-raw" and bits == 16)
        or (encoding == "raw" and bits == rate * 16 // 1000)
    ):
        raise ProtocolError("UNSUPPORTED_AUDIO_FORMAT")
    return rate


class ExotelProtocol:
    """Validate one Exotel Voicebot event stream and construct its outbound envelopes. Own
    StartInfo, stop state, and monotonic sequence/timestamp cursors. Accept only documented
    PCM16 metadata dialects; this synchronous parser has one receive-loop owner and does not
    authenticate the socket itself.
    """

    def __init__(self):
        """Initialize an unstarted protocol with no stream identifiers or audio format. parse()
        is the only path that initializes StartInfo; outbound construction requires a
        validated start.
        """
        self.start: StartInfo | None = None
        self.stopped = False
        self.sequence = -1
        self.timestamp = -1

    def parse(self, raw: str) -> dict:
        """Decode bounded JSON and validate event order, stream/call identity, counters,
        format, and PCM payload shape. Return the envelope with decoded pcm for media,
        updating protocol state only as processing advances. Reject malformed/unsupported
        input with a bounded ProtocolError; the owning connection terminates after
        rejection.
        """
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
                rate = media_sample_rate(data["media_format"])
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
        """Build a media envelope for validated-start output containing base64 raw PCM16.
        Reject packets outside 3200–100000 bytes or not a multiple of 320. Rate conversion
        and padding belong to the response pipeline.
        """
        if len(pcm) < 3200 or len(pcm) > 100_000 or len(pcm) % 320:
            raise ProtocolError("INVALID_OUTPUT_CHUNK")
        return {
            "event": "media",
            "stream_sid": self.start.stream_sid,
            "media": {"payload": base64.b64encode(pcm).decode("ascii")},
        }

    def mark(self, name: str):
        """Build an outbound playback-position mark for this initialized stream. Playback
        supplies its bounded generation-derived name and tracks the acknowledgment
        separately.
        """
        return {"event": "mark", "stream_sid": self.start.stream_sid, "mark": {"name": name}}

    def clear(self):
        """Build an outbound clear envelope for this initialized stream. The caller must
        serialize it with media and invalidate local queues/marks to prevent stale playback.
        """
        return {"event": "clear", "stream_sid": self.start.stream_sid}
