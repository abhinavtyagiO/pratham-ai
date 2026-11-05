import asyncio

from app.domain.conversations import ResponseDraft
from app.providers.interfaces import ModelResult
from app.realtime.audio import tone
from app.realtime.contracts import SpeechEvent, VoiceProviders


class ScriptedPCMSTT:
    """An audio-driven test double: scripts label VAD-delimited PCM; it is not ASR."""

    name = "scripted_pcm_stt"

    def __init__(self, utterances=None):
        self.utterances = utterances or ["MRF 195/65 R15 ka rate kya hai?", "Bhaiya rate bata do"]
        self.queue = asyncio.Queue(maxsize=16)
        self.count = 0
        self.samples = 0
        self.active = False

    async def start(self):
        pass

    async def audio(self, pcm16):
        self.samples += len(pcm16) // 2

    async def boundary(self, kind):
        if kind == "speech_start":
            self.active = True
            text = self.utterances[min(self.count, len(self.utterances) - 1)]
            await self.queue.put(SpeechEvent("partial", text[:8], str(self.count)))
        elif kind == "speech_end" and self.active:
            self.active = False
            text = self.utterances[min(self.count, len(self.utterances) - 1)]
            await self.queue.put(SpeechEvent("final", text, str(self.count)))
            self.count += 1

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self):
        pass


class MockAsyncModel:
    name = "mock"
    model = "deterministic-v1"

    async def generate(self, context):
        return ModelResult(ResponseDraft())

    async def close(self):
        pass


class ToneTTS:
    """Valid audible PCM test output; never claims to be synthesized human speech."""

    name = "tone_tts"

    async def stream(self, text, sample_rate):
        for _ in range(3):
            yield tone(0.1, sample_rate)
            await asyncio.sleep(0)

    async def close(self):
        pass


def mock_voice_providers(utterances=None):
    return VoiceProviders(ScriptedPCMSTT(utterances), MockAsyncModel(), ToneTTS())
