import argparse
import asyncio
import base64
import json
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config

from app.observability.telemetry import Telemetry, configure_logging
from app.persistence.database import make_engine, make_sessions
from app.realtime.harness import AudioHarness
from app.seed import seed


async def scenario(sessions, telemetry):
    harness = await AudioHarness(sessions, telemetry).begin()
    for _ in range(2):
        await harness.utterance()
        await harness.reply()
    detail = await harness.finish()
    assert detail["outcome"]["outcome"] == "RESOLVED", detail
    assert len(detail["voice_turns"]) == 2
    pcm = b"".join(
        base64.b64decode(m["media"]["payload"])
        for m in harness.socket.sent
        if m["event"] == "media"
    )
    return detail, pcm


def main():
    parser = argparse.ArgumentParser(
        description="Exotel/Pipecat real-PCM simulation; no credentials"
    )
    parser.add_argument("--output", type=Path, default=Path("reports/audio-simulation"))
    args = parser.parse_args()
    configure_logging()
    telemetry = Telemetry()
    with TemporaryDirectory(prefix="pratham-audio-") as temp:
        url = f"sqlite:///{Path(temp) / 'calls.db'}"
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.attributes["database_url"] = url
        command.upgrade(cfg, "head")
        engine = make_engine(url)
        sessions = make_sessions(engine)
        with sessions() as db:
            seed(db)
        try:
            detail, pcm = asyncio.run(scenario(sessions, telemetry))
        finally:
            engine.dispose()
            telemetry.shutdown()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "call.json").write_text(
        json.dumps(detail, default=str, ensure_ascii=False, indent=2)
    )
    with wave.open(str(args.output / "playback.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    print(
        json.dumps(
            {
                "call_id": detail["id"],
                "outcome": detail["outcome"]["outcome"],
                "turns": len(detail["voice_turns"]),
                "audio": str(args.output / "playback.wav"),
                "simulation": "scripted STT and audible test tones, no real providers",
            }
        )
    )


if __name__ == "__main__":
    main()
