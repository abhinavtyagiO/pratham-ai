"""Explicitly simulated audio-driven providers; none claims live recognition or speech
synthesis.
"""

import asyncio

from app.domain.conversations import ResponseDraft
from app.providers.interfaces import ModelResult
from app.realtime.audio import tone
from app.realtime.contracts import SpeechEvent, VoiceProviders


class ScriptedPCMSTT:
    """Per-call audio-driven test double that labels manual VAD-delimited PCM with scripted
    text. Own a bounded event queue, script index, sample count, and active flag. It
    performs no speech recognition or network access; only energy/manual VAD can drive it.
    """

    name = "scripted_pcm_stt"

    def __init__(self, utterances=None):
        """Retain synthetic utterances or demo price questions and allocate an empty bounded
        event queue. Script position and counters are independent for each call.
        """
        self.utterances = utterances or ["MRF 195/65 R15 ka rate kya hai?", "Bhaiya rate bata do"]
        self.queue = asyncio.Queue(maxsize=16)
        self.count = 0
        self.samples = 0
        self.active = False

    async def start(self):
        """Satisfy the recognizer startup contract without acquiring network resources. The
        queue becomes active when boundary() receives manual turn events.
        """
        pass

    async def audio(self, pcm16):
        """Count whole PCM16 samples for harness assertions; never decode text from the audio
        or claim ASR accuracy.
        """
        self.samples += len(pcm16) // 2

    async def boundary(self, kind):
        """Emit a scripted partial on speech_start and one final on an active speech_end.
        Advance the script only after a final; bounded queue puts preserve backpressure
        instead of dropping turns.
        """
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
        """Yield queued synthetic SpeechEvents until a sentinel is encountered. The owning
        session normally cancels this reader during cleanup.
        """
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self):
        """Complete the provider lifecycle without external cleanup. The session owns
        cancellation of the queue reader.
        """
        pass


class MockAsyncModel:
    """Stateless asynchronous phrasing stub for the real-time harness. Returns only the same
    approved ResponseDraft schema as the production model; it never contacts a model
    provider.
    """

    name = "mock"
    model = "deterministic-v1"

    async def generate(self, context):
        """Return the deterministic default approved draft wrapped in ModelResult. Authorized
        context is accepted for interface parity; no tokens, free text, or network requests
        are generated.
        """
        return ModelResult(ResponseDraft())

    async def close(self):
        """Complete lifecycle cleanup for a model double that owns no clients or background
        tasks.
        """
        pass


class ToneTTS:
    """Stateless PCM16 tone generator for playback and cancellation testing. Output is audible
    signal data, not synthesized human speech; each async iteration yields cooperatively so
    concurrent tasks can interrupt it.
    """

    name = "tone_tts"

    async def stream(self, text, sample_rate):
        """Yield three 100 ms mono tone chunks at sample_rate for already-authorized text. The
        content does not encode that text; cooperative yields make cancellation/backpressure
        tests deterministic.
        """
        for _ in range(3):
            yield tone(0.1, sample_rate)
            await asyncio.sleep(0)

    async def close(self):
        """Complete lifecycle cleanup without touching another utterance; the generator owns no
        external resources.
        """
        pass


def mock_voice_providers(utterances=None):
    """Create a fresh scripted recognizer, stateless draft model, and tone synthesizer for one
    call. Each bundle has independent queued transcript/script state and performs no network
    calls.
    """
    return VoiceProviders(ScriptedPCMSTT(utterances), MockAsyncModel(), ToneTTS())
