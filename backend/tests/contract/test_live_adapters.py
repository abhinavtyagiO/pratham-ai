import asyncio
import base64
import io
import json
import wave
from collections import deque
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.domain.conversations import ModelContext
from app.domain.types import Action
from app.persistence.database import make_sessions
from app.providers.deepseek import DeepSeekFlash
from app.providers.sarvam_realtime import SarvamRealtimeSTT, SarvamStreamingTTS
from app.realtime.audio import tone
from app.realtime.config import VoiceSettings
from app.realtime.contracts import VoiceFailure, VoiceProviders
from app.realtime.factory import validate_live_settings
from app.realtime.harness import AudioHarness


def live_settings(**changes):
    return VoiceSettings(
        mode="live",
        pace_audio=False,
        sarvam_api_key=SecretStr("sarvam-test-secret"),
        deepseek_api_key=SecretStr("deepseek-test-secret"),
        exotel_ws_password=SecretStr("ws-test-secret"),
        exotel_account_sid="AC-demo",
        exotel_to_number="+918000000000",
        **changes,
    )


def wav_audio(rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(tone(0.2, rate))
    return buffer.getvalue()


class FakeSocket:
    """Deterministic provider-wire peer holding scripted JSON responses, captured writes, and
    close state. No network connection exists; each connector invocation creates a separate
    instance.
    """

    def __init__(self, responses):
        """Encode scripted response envelopes and initialize independent send/close evidence
        for one fake connection.
        """
        self.responses = deque(json.dumps(x) for x in responses)
        self.sent = []
        self.closed = False

    async def send(self, data):
        """Capture decoded client JSON to validate the exact provider request contract without
        logging headers or real credentials.
        """
        self.sent.append(json.loads(data))

    async def recv(self):
        """Return the next scripted response for handshake validation, raising if the fixture
        omitted a required event.
        """
        return self.responses.popleft()

    def __aiter__(self):
        """Expose the peer as the async receive iterator expected by production adapters."""
        return self

    async def __anext__(self):
        """Yield the next scripted response or finish the stream when the script is exhausted."""
        if not self.responses:
            raise StopAsyncIteration
        return self.responses.popleft()

    async def close(self):
        """Record that the production adapter released its owned connection on success, error,
        or cancellation.
        """
        self.closed = True


class Connection:
    """Fake websockets connector result supporting both await and async-with acquisition
    styles. Own a single fake socket and close it when an utterance context exits.
    """

    def __init__(self, socket):
        """Bind one fake socket to an acquisition wrapper without starting a task."""
        self.socket = socket

    def __await__(self):
        """Return an awaitable that resolves to the fake socket for Saaras's explicit session
        ownership pattern.
        """

        async def connected():
            """Complete fake connection acquisition without contacting a provider or altering
            response order.
            """
            return self.socket

        return connected().__await__()

    async def __aenter__(self):
        """Return the bound peer for Bulbul's utterance-scoped socket context."""
        return self.socket

    async def __aexit__(self, *args):
        """Close the peer even when the synthesizer generator is cancelled or fails validation."""
        await self.socket.close()


class Connector:
    """Factory double capturing provider URL, options, and independent socket per acquisition.
    Synthetic fixture keys let tests verify header placement without using development
    secrets.
    """

    def __init__(self, factory):
        """Retain the fake socket factory and allocate per-test connection history."""
        self.factory = factory
        self.calls = []

    def __call__(self, url, **kwargs):
        """Create one isolated peer and record the production adapter's connection contract,
        returning an await/context-compatible wrapper.
        """
        socket = self.factory()
        self.calls.append((url, kwargs, socket))
        return Connection(socket)


async def test_saaras_v4_realtime_contract():
    connector = Connector(
        lambda: FakeSocket(
            [
                {"event": "session.begin", "request_id": "one"},
                {"event": "vad.speech_start"},
                {"event": "transcript.partial", "text": "एमआरएफ"},
                {"event": "vad.speech_end"},
                {"event": "transcript.final", "text": "एमआरएफ की कीमत?", "utterance_id": "one"},
                {"event": "session.end"},
            ]
        )
    )
    settings = live_settings(vad_mode="sarvam")
    stt = SarvamRealtimeSTT(settings, connector=connector)
    await stt.start()
    await stt.audio(tone(0.02))
    speech = [e async for e in stt.events()]
    await stt.close()
    url, options, socket = connector.calls[0]
    assert urlparse(url).path == "/speech-to-text-realtime/ws"
    query = parse_qs(urlparse(url).query)
    assert query["model"] == ["saaras:v4"] and query["endpointing"] == ["vad"]
    assert query["sample_rate"] == ["16000"]
    assert query["silence_duration_ms"] == ["700"]
    assert options["additional_headers"] == {"api-subscription-key": "sarvam-test-secret"}
    assert [e.kind for e in speech] == ["speech_start", "partial", "speech_end", "final"]
    assert speech[-1].utterance_id == "one"
    assert socket.sent[0] == {
        "event": "audio_input",
        "audio": base64.b64encode(tone(0.02)).decode(),
    }
    assert socket.sent[-1] == {"event": "end"} and socket.closed


async def test_stt_errors_are_sanitized_and_closed():
    connector = Connector(
        lambda: FakeSocket(
            [
                {"event": "session.begin"},
                {"event": "error", "code": "bad_key", "is_fatal": True, "message": "PRIVATE_TOKEN"},
            ]
        )
    )
    stt = SarvamRealtimeSTT(live_settings(), connector=connector)
    await stt.start()
    with pytest.raises(VoiceFailure) as raised:
        _ = [e async for e in stt.events()]
    assert "PRIVATE" not in str(raised.value)
    await stt.close()


def audio_messages(wav):
    # Fragment the RIFF header as well as samples; the adapter must not play header bytes.
    sizes = [7, 13, 21, 101, len(wav)]
    offset, messages = 0, []
    for size in sizes:
        data = wav[offset : offset + size]
        offset += size
        if data:
            messages.append(
                {
                    "type": "audio",
                    "data": {"audio": base64.b64encode(data).decode(), "content_type": "audio/wav"},
                }
            )
    messages.append({"type": "event", "data": {"event_type": "final"}})
    return messages


async def test_bulbul_v4_flash_contract_and_wav_to_pcm():
    connector = Connector(lambda: FakeSocket(audio_messages(wav_audio())))
    tts = SarvamStreamingTTS(live_settings(), connector=connector)
    pcm = b"".join([chunk async for chunk in tts.stream("नमस्ते", 16000)])
    assert pcm == tone(0.2) and not pcm.startswith(b"RIFF")
    url, options, socket = connector.calls[0]
    assert parse_qs(urlparse(url).query)["model"] == ["bulbul:v4-flash"]
    assert socket.sent[0]["data"]["output_audio_codec"] == "wav"
    assert socket.sent[0]["data"]["speech_sample_rate"] == 16000
    assert socket.sent[0]["data"]["speaker"] == "shubh_hi_customer"
    assert [m["type"] for m in socket.sent] == ["config", "text", "flush"]
    assert socket.closed


@pytest.mark.parametrize("bad_audio", [b"not a wave file", wav_audio(8000), wav_audio()[:-1]])
async def test_bad_tts_audio_never_passes_as_playable_pcm(bad_audio):
    connector = Connector(lambda: FakeSocket(audio_messages(bad_audio)))
    with pytest.raises(VoiceFailure):
        _ = [
            chunk
            async for chunk in SarvamStreamingTTS(live_settings(), connector=connector).stream(
                "नमस्ते", 16000
            )
        ]


def sse_response(
    content='{"phrasing":"BRIEF","mention_closing_time":true}', finish="stop", done=True
):
    events = [
        {"choices": [{"delta": {"content": content[:10]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": content[10:]}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 25, "completion_tokens": 12}},
    ]
    data = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    return data + ("data: [DONE]\n\n" if done else "")


async def test_deepseek_flash_non_thinking_json_gate_and_usage():
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, text=sse_response(), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    )
    model = DeepSeekFlash(live_settings(), client=client)
    result = await model.generate(
        ModelContext(decision=Action.DO_NOT_DISCLOSE_PRICE, closing_time_phrase="shaam 6:30")
    )
    await model.close()
    assert result.value.phrasing == "BRIEF" and result.input_tokens == 25
    body = seen[0]
    assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body
    assert set(json.loads(body["messages"][1]["content"])) == {"decision", "closing_time_phrase"}


@pytest.mark.parametrize(
    "content,finish,done",
    [
        ('{"text":"Tyre costs ₹5000"}', "stop", True),
        (" ", "stop", True),
        ('{"phrasing":"POLITE"}', "length", True),
        ('{"phrasing":"BRIEF"}', "stop", False),
    ],
)
async def test_deepseek_rejects_blank_injected_or_truncated_output(content, finish, done):
    client = httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=sse_response(content, finish, done))
        ),
    )
    model = DeepSeekFlash(live_settings(), client=client)
    with pytest.raises((VoiceFailure, ValidationError)):
        await model.generate(ModelContext(decision=Action.DO_NOT_DISCLOSE_PRICE))
    await model.close()


def test_live_config_fails_closed_without_credentials():
    with pytest.raises(ValueError, match="sarvam_api_key"):
        validate_live_settings(VoiceSettings(mode="live"))


async def test_bulbul_cancellation_closes_the_obsolete_socket():
    entered = asyncio.Event()

    class BlockingSocket(FakeSocket):
        """Provider peer that emits initial audio then stalls, exposing whether cancellation
        closes the obsolete utterance socket.
        """

        async def __anext__(self):
            """Return scripted chunks, then signal the blocked state and await cancellation
            instead of ending synthesis normally.
            """
            if self.responses:
                return self.responses.popleft()
            entered.set()
            await asyncio.Event().wait()

    connector = Connector(lambda: BlockingSocket(audio_messages(wav_audio())[:-1]))
    tts = SarvamStreamingTTS(live_settings(), connector=connector)

    async def consume():
        async for _ in tts.stream("नमस्ते", 16000):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connector.calls[0][2].closed


async def test_actual_adapters_across_multiturn_pipeline_with_mocked_network(
    engine, session, telemetry
):
    class SpeechSocket(FakeSocket):
        """Manual-endpointing Saaras peer for the energy-VAD multi-turn contract test. Own an
        async queue and turn counter; real production adapters still parse every wire event.
        """

        def __init__(self):
            """Queue session.begin and initialize independent transcript/turn state for the
            fake call.
            """
            super().__init__([])
            self.queue = asyncio.Queue()
            self.queue.put_nowait(json.dumps({"event": "session.begin"}))
            self.turn = 0

        async def recv(self):
            """Wait for the next provider event through the same queue used by the long-running
            reader.
            """
            return await self.queue.get()

        async def __anext__(self):
            """Keep the fake provider stream open between turns until the session cancels its
            reader.
            """
            return await self.queue.get()

        async def send(self, raw):
            """Capture client events and translate manual speech boundaries into scripted
            partial/final transcripts with unique utterance IDs.
            """
            await super().send(raw)
            data = json.loads(raw)
            if data["event"] == "speech_start":
                self.queue.put_nowait(json.dumps({"event": "transcript.partial", "text": "एमआरएफ"}))
            elif data["event"] == "speech_end":
                self.turn += 1
                self.queue.put_nowait(
                    json.dumps(
                        {
                            "event": "transcript.final",
                            "text": "एमआरएफ का रेट क्या है?",
                            "utterance_id": str(self.turn),
                        }
                    )
                )

    settings = live_settings()
    speech_connector = Connector(SpeechSocket)
    tts_connector = Connector(lambda: FakeSocket(audio_messages(wav_audio())))
    client = httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse_response())),
    )
    providers = VoiceProviders(
        SarvamRealtimeSTT(settings, connector=speech_connector),
        DeepSeekFlash(settings, client=client),
        SarvamStreamingTTS(settings, connector=tts_connector),
    )
    harness = await AudioHarness(
        make_sessions(engine), telemetry, settings=settings, providers=providers
    ).begin()
    for _ in range(2):
        await harness.utterance()
        await harness.reply()
    detail = await harness.finish()
    assert detail["outcome"]["outcome"] == "RESOLVED"
    assert len(detail["voice_turns"]) == 2 and len(tts_connector.calls) == 3
    assert all(m["prompt_version"] == "response_generation:v2" for m in detail["model_invocations"])
    assert all(
        "रेट" in m["text"]
        for m in detail["messages"]
        if m["speaker"] == "ASSISTANT" and m["turn_index"]
    )
    assert all(
        m["delivery_status"] == "PLAYED" for m in detail["messages"] if m["speaker"] == "ASSISTANT"
    )
