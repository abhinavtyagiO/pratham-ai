"""Production Saaras realtime STT and Bulbul streaming TTS wire adapters."""

import asyncio
import base64
import json
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from app.realtime.contracts import SpeechEvent, VoiceFailure
from app.realtime.wav import StreamingWAV


class SarvamRealtimeSTT:
    """One call's Saaras realtime WebSocket adapter, producing normalized SpeechEvents. Own the
    provider socket, application keepalive task, and send lock; the session owns the receive
    loop and stall deadlines. Start once and close once per call; no reconnect can silently
    replay caller audio. Injectable connectors support wire-contract tests without network
    access.
    """

    name = "sarvam_realtime"
    endpoint = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

    def __init__(self, settings, *, connector=connect):
        """Retain voice settings and connector, and allocate an empty socket/keepalive plus
        send lock. Credentials remain in SecretStr and are unwrapped only for provider
        authentication in start().
        """
        self.settings, self.connector = settings, connector
        self.socket = None
        self.keepalive = None
        self.send_lock = asyncio.Lock()

    def url(self):
        """Build the explicit Saaras v4 realtime query for 16 kHz linear16 and configured
        Hindi/VAD tuning. Server mode uses endpointing=vad; energy mode uses manual
        boundaries. No credentials are included in the URL.
        """
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
        """Connect with the subscription-key header and bounded WebSocket buffers, then require
        session.begin before starting keepalive. The session wraps this operation in a
        deadline and owns close() even if handshake parsing fails.
        """
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
        """Serialize one JSON provider event under the send lock shared by audio, boundaries,
        ping, and end. Errors propagate to the caller's deadline/failure handling; payloads
        are never logged here.
        """
        async with self.send_lock:
            await self.socket.send(json.dumps(data))

    async def audio(self, pcm16):
        """Encode internal raw 16 kHz mono PCM16 as the realtime audio_input envelope. Await
        the serialized socket write; caller cancellation and send errors propagate.
        """
        await self.send({"event": "audio_input", "audio": base64.b64encode(pcm16).decode("ascii")})

    async def boundary(self, kind):
        """Send manual speech_start/end only in energy mode. Server VAD already owns turn
        boundaries, so forwarding client boundaries in that mode would conflict with
        provider endpointing.
        """
        if self.settings.vad_mode == "energy":
            await self.send({"event": kind})

    async def ping(self):
        """Send application keepalives every ten seconds under a provider deadline. On
        transport timeout/error close the socket so the receive loop reports failure;
        close() cancels and joins this task.
        """
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
        """Yield normalized server VAD and partial/final transcript events from the provider
        socket. Limit text length, retain utterance IDs for deduplication, ignore expected
        control events, and terminate on session.end. Invalid/error events raise sanitized
        VoiceFailure without copying provider messages; caller cancellation propagates.
        """
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
        """Cancel/join keepalive, attempt a bounded end event, and close the owned WebSocket in
        finally. The session separately cancels the receive loop and bounds this cleanup;
        errors may propagate for safe cleanup diagnostics.
        """
        if self.keepalive:
            self.keepalive.cancel()
            await asyncio.gather(self.keepalive, return_exceptions=True)
        if self.socket:
            try:
                await asyncio.wait_for(self.send({"event": "end"}), 1)
            finally:
                await self.socket.close()


class SarvamStreamingTTS:
    """Stream Bulbul v4 Flash speech as validated 16 kHz mono PCM16. Each utterance owns a
    fresh socket through its async generator, avoiding cross-generation audio after
    cancellation. No persistent socket is shared across responses or calls.
    """

    name = "sarvam_bulbul"
    endpoint = "wss://api.sarvam.ai/text-to-speech/ws"

    def __init__(self, settings, *, connector=connect):
        """Retain explicit model/voice/language settings and an injectable WebSocket connector.
        No network resource is acquired until stream() is iterated.
        """
        self.settings, self.connector = settings, connector

    async def stream(self, text, sample_rate):
        """Yield bounded raw PCM chunks for already-authorized text at 16 kHz. Open an
        utterance-owned socket, configure WAV output, send text/flush, and validate/strip
        the streaming RIFF header. Require final completion and whole PCM samples;
        protocol/provider errors raise sanitized VoiceFailure. Generator closure or
        cancellation exits the socket context and discards obsolete audio.
        """
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
        """Satisfy the provider lifecycle interface without closing an unrelated utterance.
        Active sockets belong to stream() generators, which the session closes before
        provider cleanup.
        """
        pass
