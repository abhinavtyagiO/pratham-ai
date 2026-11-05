"""Acknowledged application frames carried by a real Pipecat pipeline."""

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
    """Pipecat input-audio frame with a producer-owned completion future. Carries internal 16
    kHz mono PCM; awaiting done prevents an unbounded backlog in Pipecat's own queue.
    """

    done: asyncio.Future = field(kw_only=True)


@dataclass
class RecognizedSpeech(DataFrame):
    """Pipecat data frame carrying one normalized STT/VAD SpeechEvent and its completion
    future. The session's recognition reader waits until the speech stage finishes
    processing it.
    """

    event: SpeechEvent
    done: asyncio.Future


@dataclass
class AuthorizedPlayback(DataFrame):
    """Pipecat data frame carrying a policy-authorized Packet and completion future. Only
    rendered speech reaches this boundary; the output processor queues it for generation
    checks.
    """

    packet: Packet
    done: asyncio.Future


class VoiceProcessor(FrameProcessor):
    """Acknowledge one application frame type while forwarding Pipecat lifecycle frames. Holds
    a per-call session, handled class, and async handler; direct mode avoids an extra
    processing queue. Producer futures carry failures back to the owning session task.
    """

    def __init__(self, session, handled_type, handler):
        """Bind a handled frame class and its async handler, retaining the per-call session.
        Direct mode keeps processing completion aligned with the producer's acknowledgement.
        """
        super().__init__(enable_direct_mode=True)
        self.session, self.handled_type, self.handler = session, handled_type, handler

    async def process_frame(self, frame, direction):
        """Run Pipecat base handling, await the matching handler, and resolve its future. Skip
        work whose producer was already cancelled; return handler exceptions through the
        future. Unhandled lifecycle/control frames continue downstream. Task cancellation
        remains cancellation.
        """
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
    """Own the real Pipecat worker/runner for one call's audio, STT events, and playback. Three
    processors dispatch to the session; startup is synchronized by an event. The session
    owns this wrapper and must start it before submit and close it during cleanup.
    """

    def __init__(self, session):
        """Build processors and a worker with internal 16 kHz rates and bounded lifecycle
        deadlines. Disable automatic RTVI and turn tracking because Exotel acknowledgments
        and the session own those semantics. No runner task starts here.
        """

        async def audio(frame):
            """Route an acknowledged input frame's raw PCM to the session's VAD/STT stage."""
            await session.audio(frame.audio)

        async def speech(frame):
            """Apply a recognized event through the session's turn and interruption state
            machine.
            """
            await session.speech(frame.event)

        async def output(frame):
            """Transfer an authorized Packet to bounded playback; generation validation remains
            the playback owner's responsibility.
            """
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
            """Signal that Pipecat's worker has started so submitters cannot race pipeline
            initialization.
            """
            self.ready.set()

    async def start(self):
        """Register the worker, launch the runner task, and wait for startup with a finite
        deadline. Store the task so cleanup can join or cancel it even if readiness times
        out.
        """
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        await asyncio.wait_for(self.ready.wait(), 6)

    async def submit(self, frame, timeout):
        """Queue a frame and await its completion future for at most timeout seconds. Propagate
        handler exceptions, timeout, and cancellation to the producer; a timed-out future
        prevents later queued application handling.
        """
        await self.worker.queue_frame(frame)
        await asyncio.wait_for(frame.done, timeout)

    async def close(self):
        """Request graceful EndFrame shutdown and join the runner. If the grace deadline
        expires, cancel the runner and wait under a second finite deadline; unresolved
        shutdown errors reach session cleanup.
        """
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 3)
        except TimeoutError:
            await self.runner.cancel()
            await asyncio.wait_for(self.task, 3)
