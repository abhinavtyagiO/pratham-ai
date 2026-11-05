import asyncio
from dataclasses import dataclass, field

from loguru import logger

# Provider/frame reprs can contain text or audio. Use only the application's safe event logger.
logger.disable("pipecat")

from pipecat.frames.frames import DataFrame, EndFrame, InputAudioRawFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from app.realtime.contracts import SpeechEvent  # noqa: E402
from app.realtime.playback import Packet  # noqa: E402


@dataclass
class IncomingPCM(InputAudioRawFrame):
    done: asyncio.Future = field(kw_only=True)


@dataclass
class RecognizedSpeech(DataFrame):
    event: SpeechEvent
    done: asyncio.Future


@dataclass
class AuthorizedPlayback(DataFrame):
    packet: Packet
    done: asyncio.Future


class VoiceProcessor(FrameProcessor):
    def __init__(self, session, handled_type, handler):
        super().__init__(enable_direct_mode=True)
        self.session, self.handled_type, self.handler = session, handled_type, handler

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, self.handled_type):
            try:
                # Producers await acknowledgement: bounded in-flight input despite Pipecat's queue.
                if not frame.done.cancelled():
                    await self.handler(frame)
                if not frame.done.done():
                    frame.done.set_result(None)
            except Exception as exc:
                if not frame.done.done():
                    frame.done.set_exception(exc)
            return
        await self.push_frame(frame, direction)


class VoicePipeline:
    def __init__(self, session):
        async def audio(frame):
            await session.audio(frame.audio)

        async def speech(frame):
            await session.speech(frame.event)

        async def output(frame):
            await session.playback.enqueue(frame.packet)

        self.worker = PipelineWorker(
            Pipeline(
                [
                    VoiceProcessor(session, IncomingPCM, audio),
                    VoiceProcessor(session, RecognizedSpeech, speech),
                    VoiceProcessor(session, AuthorizedPlayback, output),
                ]
            ),
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
            setup_timeout_secs=5,
            start_timeout_secs=5,
            cancel_timeout_secs=2,
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self.ready = asyncio.Event()

        @self.worker.event_handler("on_pipeline_started")
        async def ready(worker, frame):
            self.ready.set()

    async def start(self):
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        await asyncio.wait_for(self.ready.wait(), 6)

    async def submit(self, frame, timeout):
        await self.worker.queue_frame(frame)
        await asyncio.wait_for(frame.done, timeout)

    async def close(self):
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 3)
        except TimeoutError:
            await self.runner.cancel()
            await asyncio.wait_for(self.task, 3)
