import asyncio
import base64
import json
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from app.realtime.contracts import SpeechEvent, VoiceFailure
from app.realtime.wav import StreamingWAV


class SarvamRealtimeSTT:
    name = "sarvam_realtime"
    endpoint = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

    def __init__(self, settings, *, connector=connect):
        self.settings, self.connector = settings, connector
        self.socket = None
        self.keepalive = None
        self.send_lock = asyncio.Lock()

    def url(self):
        cfg = self.settings
        return (
            self.endpoint
            + "?"
            + urlencode(
                {
                    "model": cfg.stt_model,
                    "language_code": cfg.language_code,
                    "stream_type": "fast",
                    "mode": "codemix",
                    "encoding": "linear16",
                    "sample_rate": 16000,
                    "endpointing": "vad" if cfg.vad_mode == "sarvam" else "manual",
                    "threshold": cfg.vad_threshold,
                    "silence_duration_ms": cfg.silence_ms,
                    "min_speech_duration_ms": cfg.min_speech_ms,
                }
            )
        )

    async def start(self):
        self.socket = await self.connector(
            self.url(),
            additional_headers={
                "api-subscription-key": self.settings.sarvam_api_key.get_secret_value()
            },
            max_size=256_000,
            max_queue=16,
            open_timeout=self.settings.provider_timeout_s,
            close_timeout=1,
            ping_interval=20,
        )
        event = json.loads(await self.socket.recv())
        if event.get("event") != "session.begin":
            raise VoiceFailure("STT_ERROR", "stt_session_begin")
        self.keepalive = asyncio.create_task(self.ping())

    async def send(self, data):
        async with self.send_lock:
            await self.socket.send(json.dumps(data))

    async def audio(self, pcm16):
        await self.send({"event": "audio_input", "audio": base64.b64encode(pcm16).decode("ascii")})

    async def boundary(self, kind):
        if self.settings.vad_mode == "energy":
            await self.send({"event": kind})

    async def ping(self):
        try:
            while True:
                await asyncio.sleep(10)
                await asyncio.wait_for(
                    self.send({"event": "ping"}), self.settings.provider_timeout_s
                )
        except (OSError, TimeoutError):
            # The receive loop/session watchdog owns failure reporting and shutdown.
            await self.socket.close()

    async def events(self):
        async for raw in self.socket:
            try:
                data = json.loads(raw)
                kind = data["event"]
                if kind == "error":
                    raise VoiceFailure("STT_ERROR", "stt_provider_error", "ProviderError")
                if kind in ("vad.speech_start", "vad.speech_end"):
                    if self.settings.vad_mode == "sarvam":
                        yield SpeechEvent(kind.split(".")[1])
                elif kind in ("transcript.partial", "transcript.final"):
                    text = data["text"]
                    if not isinstance(text, str) or len(text) > 4000:
                        raise ValueError
                    utterance_id = data.get("utterance_id")
                    yield SpeechEvent(
                        kind.split(".")[1],
                        text,
                        str(utterance_id) if utterance_id is not None else None,
                    )
                elif kind == "session.end":
                    return
                elif kind not in ("pong", "config.updated", "session.begin"):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise VoiceFailure("STT_ERROR", "stt_protocol", "InvalidProviderMessage") from None

    async def close(self):
        if self.keepalive:
            self.keepalive.cancel()
            await asyncio.gather(self.keepalive, return_exceptions=True)
        if self.socket:
            try:
                await asyncio.wait_for(self.send({"event": "end"}), 1)
            finally:
                await self.socket.close()


class SarvamStreamingTTS:
    name = "sarvam_bulbul"
    endpoint = "wss://api.sarvam.ai/text-to-speech/ws"

    def __init__(self, settings, *, connector=connect):
        self.settings, self.connector = settings, connector

    async def stream(self, text, sample_rate):
        if not text.strip() or len(text) > 2500 or sample_rate != 16000:
            raise VoiceFailure("TTS_ERROR", "tts_input")
        cfg = self.settings
        url = (
            self.endpoint
            + "?"
            + urlencode({"model": cfg.tts_model, "send_completion_event": "true"})
        )
        # A fresh connection per utterance makes cancellation unambiguous: close discards all
        # obsolete audio; it cannot contaminate a subsequent response on a shared socket.
        async with self.connector(
            url,
            additional_headers={"api-subscription-key": cfg.sarvam_api_key.get_secret_value()},
            max_size=512_000,
            max_queue=8,
            open_timeout=cfg.provider_timeout_s,
            close_timeout=1,
            ping_interval=20,
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "config",
                        "data": {
                            "language_code": cfg.language_code,
                            "speaker": cfg.tts_speaker,
                            "speech_sample_rate": sample_rate,
                            "output_audio_codec": "wav",
                            "pace": 1.0,
                            "enable_preprocessing": True,
                        },
                    }
                )
            )
            await socket.send(json.dumps({"type": "text", "data": {"text": text}}))
            await socket.send(json.dumps({"type": "flush"}))
            decoder = StreamingWAV(sample_rate)
            completed = False
            async for raw in socket:
                try:
                    message = json.loads(raw)
                    if message.get("type") == "audio":
                        data = base64.b64decode(message["data"]["audio"], validate=True)
                        pcm = decoder.feed(data)
                        for offset in range(0, len(pcm), 32000):
                            yield pcm[offset : offset + 32000]
                    elif (
                        message.get("type") == "event"
                        and message["data"].get("event_type") == "final"
                    ):
                        decoder.finish()
                        completed = True
                        break
                    elif message.get("type") == "error":
                        raise VoiceFailure("TTS_ERROR", "tts_provider_error", "ProviderError")
                    else:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise VoiceFailure(
                        "TTS_ERROR", "tts_protocol", "InvalidProviderMessage"
                    ) from None
            if not completed:
                raise VoiceFailure("TTS_ERROR", "tts_disconnected")

    async def close(self):
        # Each active utterance is owned by and closed with its async generator.
        pass
