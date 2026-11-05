"""Both official metadata dialects must resolve to identical validated PCM sessions."""

import base64
import json

import pytest

from app.realtime.harness import start_event
from app.realtime.protocol import ExotelProtocol, ProtocolError


@pytest.mark.parametrize("rate", [8000, 16000, 24000])
@pytest.mark.parametrize("encoding", ["audio/x-raw", "raw"])
@pytest.mark.parametrize("as_string", [False, True])
def test_documented_metadata_variants_preserve_event_contract(rate, encoding, as_string):
    event = start_event(sample_rate=rate)
    bits = 16 if encoding == "audio/x-raw" else rate * 16 // 1000
    event["start"]["media_format"] = {
        "encoding": encoding,
        "sample_rate": str(rate) if as_string else rate,
        "bit_rate": str(bits) if as_string else bits,
    }
    protocol = ExotelProtocol()
    protocol.parse(json.dumps(event))
    assert protocol.start.sample_rate == rate
    pcm = b"\1\0" * 1600
    media = {
        "event": "media",
        "sequence_number": 2,
        "stream_sid": "MZ-demo",
        "media": {"payload": base64.b64encode(pcm).decode(), "timestamp": 0},
    }
    assert protocol.parse(json.dumps(media))["pcm"] == pcm
    assert protocol.media(pcm)["media"]["payload"] == media["media"]["payload"]
    for invalid in (event, media, {**media, "sequence_number": 3, "stream_sid": "OTHER"}):
        with pytest.raises(ProtocolError):
            protocol.parse(json.dumps(invalid))
    protocol.parse(
        json.dumps({"event": "stop", "stream_sid": "MZ-demo", "stop": {"call_sid": "CA-demo"}})
    )
    with pytest.raises(ProtocolError):
        protocol.parse(json.dumps(media))


@pytest.mark.parametrize(
    "changes",
    [
        {"encoding": "raw", "bit_rate": 16},
        {"encoding": "audio/x-raw", "bit_rate": 128},
        {"encoding": "raw", "bit_rate": 256},
        {"encoding": "raw", "bit_rate": 128, "sample_rate": 16000},
        {"encoding": "mulaw"},
        {"encoding": "audio/x-alaw"},
        {"encoding": "wav"},
        {"sample_rate": 48000},
        {"sample_rate": 8000.5},
        {"sample_rate": True},
        {"bit_rate": 16.5},
        {"bit_rate": True},
        {"channels": 2},
        {"channels": True},
    ],
)
def test_ambiguous_compressed_stereo_and_malformed_formats_fail_closed(changes):
    event = start_event()
    event["start"]["media_format"].update(changes)
    protocol = ExotelProtocol()
    with pytest.raises(ProtocolError, match="UNSUPPORTED_AUDIO_FORMAT"):
        protocol.parse(json.dumps(event))
    assert protocol.start is None


@pytest.mark.parametrize("fmt", [None, [], {}, {"encoding": "raw"}])
def test_incomplete_format_never_initializes_session(fmt):
    event = start_event()
    event["start"]["media_format"] = fmt
    with pytest.raises(ProtocolError):
        ExotelProtocol().parse(json.dumps(event))
