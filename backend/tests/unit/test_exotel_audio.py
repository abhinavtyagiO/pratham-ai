import base64
import json

import numpy as np
import pytest

from app.realtime.audio import EnergyVAD, PCMBuffer, PCMResampler, tone
from app.realtime.harness import start_event
from app.realtime.protocol import ExotelProtocol, ProtocolError


def started(rate=8000):
    protocol = ExotelProtocol()
    protocol.parse(json.dumps({"event": "connected"}))
    protocol.parse(json.dumps(start_event(sample_rate=rate)))
    return protocol


@pytest.mark.parametrize("rate", [8000, 16000, 24000])
def test_documented_exotel_events_and_pcm_rates(rate):
    protocol = started(rate)
    pcm = tone(0.2, rate)
    media = protocol.parse(
        json.dumps(
            {
                "event": "media",
                "sequence_number": "2",
                "stream_sid": "MZ-demo",
                "media": {
                    "payload": base64.b64encode(pcm).decode(),
                    "timestamp": "0",
                    "chunk": "1",
                },
            }
        )
    )
    assert media["pcm"] == pcm
    packet = protocol.media(b"\0" * 3200)
    assert set(packet) == {"event", "stream_sid", "media"}
    assert protocol.clear() == {"event": "clear", "stream_sid": "MZ-demo"}
    protocol.parse(
        json.dumps({"event": "mark", "stream_sid": "MZ-demo", "mark": {"name": "reply-1"}})
    )
    protocol.parse(json.dumps({"event": "dtmf", "stream_sid": "MZ-demo", "dtmf": {"digit": "5"}}))
    protocol.parse(
        json.dumps({"event": "stop", "stream_sid": "MZ-demo", "stop": {"call_sid": "CA-demo"}})
    )
    assert protocol.stopped
    with pytest.raises(ProtocolError):
        protocol.parse(json.dumps({"event": "connected"}))


@pytest.mark.parametrize(
    "changes", [{"encoding": "audio/x-mulaw"}, {"sample_rate": "44100"}, {"bit_rate": "8"}]
)
def test_wrong_codec_or_rate_is_rejected(changes):
    event = start_event()
    event["start"]["media_format"].update(changes)
    with pytest.raises(ProtocolError):
        ExotelProtocol().parse(json.dumps(event))


@pytest.mark.parametrize(
    "message",
    [
        "not JSON",
        "[]",
        json.dumps({"event": "clear"}),
        json.dumps({"event": "media", "stream_sid": "WRONG", "media": {"payload": "AA=="}}),
        json.dumps({"event": "media", "stream_sid": "MZ-demo", "media": {"payload": "!"}}),
        json.dumps({"event": "media", "stream_sid": "MZ-demo", "media": {"payload": "AA=="}}),
        json.dumps(
            {
                "event": "mark",
                "sequence_number": "1",
                "stream_sid": "MZ-demo",
                "mark": {"name": "old"},
            }
        ),
    ],
)
def test_protocol_rejects_malformed_cross_stream_and_replayed_events(message):
    with pytest.raises(ProtocolError):
        started().parse(message)


@pytest.mark.parametrize("source,target", [(8000, 16000), (24000, 16000), (16000, 8000)])
def test_streaming_resampler_preserves_duration_and_signal(source, target):
    pcm = tone(0.4, source)
    converter = PCMResampler(source, target)
    output = b"".join(converter.convert(pcm[i : i + 320]) for i in range(0, len(pcm), 320))
    output += converter.convert(b"", final=True)
    assert abs(len(output) // 2 - round(0.4 * target)) <= 2
    assert np.max(np.abs(np.frombuffer(output, dtype="<i2"))) > 1000


def test_buffer_handles_partial_frames_and_pads_final_exotel_packet():
    buffer = PCMBuffer(3200)
    assert buffer.feed(b"\1\0" * 100) == []
    assert len(buffer.feed(b"\0" * 6200)) == 2
    assert buffer.finish(pad=True) == b""
    buffer.feed(b"\1\0")
    final = buffer.finish(pad=True)
    assert len(final) == 3200 and final[:2] == b"\1\0"


def test_vad_minimum_speech_and_slow_speech_pause():
    vad = EnergyVAD(500, 100, 700)
    speech = tone(0.02)
    assert [vad.update(speech) for _ in range(4)] == [None] * 4
    assert vad.update(speech) == "speech_start"
    assert all(vad.update(b"\0" * 640) is None for _ in range(20))
    assert vad.update(speech) is None
    assert all(vad.update(b"\0" * 640) is None for _ in range(34))
    assert vad.update(b"\0" * 640) == "speech_end"
