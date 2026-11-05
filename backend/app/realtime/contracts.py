from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.conversations import ModelContext, ResponseDraft
from app.providers.interfaces import ModelResult


@dataclass(frozen=True)
class SpeechEvent:
    kind: Literal["speech_start", "speech_end", "partial", "final"]
    text: str = ""
    utterance_id: str | None = None


class StreamingSTT(Protocol):
    name: str

    async def start(self): ...
    async def audio(self, pcm16: bytes): ...
    async def boundary(self, kind: str): ...
    def events(self) -> AsyncIterator[SpeechEvent]: ...
    async def close(self): ...


class AsyncResponseModel(Protocol):
    name: str
    model: str

    async def generate(self, context: ModelContext) -> ModelResult[ResponseDraft]: ...
    async def close(self): ...


class StreamingTTS(Protocol):
    name: str

    def stream(self, text: str, sample_rate: int) -> AsyncIterator[bytes]: ...
    async def close(self): ...


@dataclass
class VoiceProviders:
    stt: StreamingSTT
    model: AsyncResponseModel
    tts: StreamingTTS


class VoiceFailure(Exception):
    def __init__(self, category: str, stage: str, error_type: str = "VoiceFailure"):
        self.category, self.stage, self.error_type = category, stage, error_type
        super().__init__(category)
