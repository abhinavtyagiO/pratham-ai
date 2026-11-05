"""Explicitly opted-in provider checks using synthetic speech and policy-limited context.

This command is separate from pytest and never opens an Exotel call or HTTP
server. It uses the production adapters, records sanitized stage diagnostics,
and writes a playable WAV. Nothing runs on import or without ``--live``.
"""

import argparse
import asyncio
import hashlib
import json
import math
import struct
import sys
import wave
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import httpx
from websockets.exceptions import InvalidStatus

from app.domain.conversations import ModelContext, ResponseDraft
from app.domain.types import Action
from app.providers.deepseek import DeepSeekFlash
from app.providers.sarvam_realtime import SarvamRealtimeSTT, SarvamStreamingTTS
from app.realtime.config import VoiceSettings
from app.realtime.contracts import VoiceFailure

SYNTHETIC_TEXT = "नमस्ते। यह प्रथम एआई का परीक्षण है। मुझे टायर का रेट बताइए।"
RATE = 16000


def safe_error(exc):
    """Return bounded diagnostics without exception messages, URLs, bodies, or headers.

    Provider errors may echo tokens or customer data, so even seemingly helpful
    exception strings must not be copied into logs or the saved report.
    """
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, VoiceFailure):
        result.update(category=exc.category, stage=exc.stage)
    elif isinstance(exc, httpx.HTTPStatusError):
        result["http_status"] = exc.response.status_code
    elif isinstance(exc, InvalidStatus):
        result["http_status"] = exc.response.status_code
    return result


async def check_tts(settings, output):
    """Synthesize the fixed Hindi test sentence and save validated mono PCM16 as WAV.

    Returns PCM plus latency/level diagnostics. The caller bounds the entire
    check; cancellation closes the generator's owned socket. Excessive, silent,
    empty, or malformed audio fails instead of producing a misleading pass.
    """
    tts = SarvamStreamingTTS(settings)
    started, first, pcm = monotonic(), None, bytearray()
    async with aclosing(tts.stream(SYNTHETIC_TEXT, RATE)) as chunks:
        async for chunk in chunks:
            if not isinstance(chunk, bytes) or len(chunk) % 2:
                raise VoiceFailure("TTS_ERROR", "smoke_invalid_pcm")
            if chunk:
                first = first if first is not None else monotonic() - started
                pcm.extend(chunk)
            if len(pcm) > RATE * 2 * 30:
                raise VoiceFailure("TTS_ERROR", "smoke_audio_limit")
    samples = [s[0] for s in struct.iter_unpack("<h", pcm)]
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
    if len(samples) < RATE // 4 or rms < 5:
        raise VoiceFailure("TTS_ERROR", "smoke_empty_or_silent_audio")
    with wave.open(str(output), "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    return bytes(pcm), {
        "first_audio_ms": round(first * 1000, 2),
        "duration_s": round(len(samples) / RATE, 3),
        "pcm_bytes": len(pcm),
        "rms": round(rms, 2),
        "audio": str(output),
    }


async def check_stt(settings, pcm):
    """Stream the TTS sample at real-time speed through Saaras server-side VAD.

    Silence brackets the synthetic utterance; no manual boundaries are sent.
    Returns event counts and final-text length/hash, without logging provider
    text. Requires a nonempty final and both VAD boundaries, not exact wording.
    Sender and reader run concurrently and are cancelled/joined before cleanup.
    """
    stt = SarvamRealtimeSTT(settings.model_copy(update={"vad_mode": "sarvam"}))
    counts, final, tasks = {}, None, []

    async def send():
        """Pace 100 ms PCM chunks so server VAD sees a realistic, bounded utterance."""
        trailing_s = max(1.5, settings.silence_ms / 1000 * 2)
        audio = b"\0" * (RATE * 2 // 3) + pcm + b"\0" * round(RATE * 2 * trailing_s)
        for offset in range(0, len(audio), 3200):
            await stt.audio(audio[offset : offset + 3200])
            await asyncio.sleep(0.1)

    async def receive():
        """Stop only at a usable final; partials are evidence, never recognized turns."""
        nonlocal final
        async for event in stt.events():
            counts[event.kind] = counts.get(event.kind, 0) + 1
            if event.kind == "final" and event.text.strip():
                final = event.text
                return
        raise VoiceFailure("STT_ERROR", "smoke_no_final")

    try:
        await stt.start()
        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        await asyncio.gather(*tasks)
        if not counts.get("speech_start") or not counts.get("speech_end"):
            raise VoiceFailure("STT_ERROR", "smoke_missing_vad")
        return {
            "event_counts": counts,
            "final_text_length": len(final),
            "final_text_sha256": hashlib.sha256(final.encode()).hexdigest(),
            "input": "synthetic Bulbul-generated Hindi speech",
            "endpointing": "vad",
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(stt.close(), settings.provider_timeout_s)


async def check_model(settings):
    """Request a closed-schema phrasing choice with thinking explicitly disabled.

    Uses synthetic authorized context only. Returns the validated draft and
    token counts; the client is closed on success, failure, or cancellation.
    The utility does not apply a fallback that could mask a failed live request.
    """
    model = DeepSeekFlash(settings)
    try:
        context = ModelContext(
            decision=Action.DO_NOT_DISCLOSE_PRICE, closing_time_phrase="shaam 6:30"
        )
        result = await model.generate(context)
        draft = ResponseDraft.model_validate(result.value)
        return {
            "thinking": "disabled",
            "validated_draft": draft.model_dump(mode="json"),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
    finally:
        await model.close()


async def run_checks(settings, output_dir, timeout_s):
    """Run three live checks independently, except STT requires successful speech input.

    Missing credentials yield BLOCKED without connecting. Each attempted stage
    has its own deadline and returns FAILED safely on error; cancellation of
    the command still propagates. This function performs network requests and
    must only be called after the CLI's explicit opt-in gate.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "execution": "LIVE",
        "created_at": datetime.now(UTC).isoformat(),
        "synthetic_data_only": True,
        "exotel_requests_performed": False,
        "real_phone_acceptance": "NOT_TESTED",
        "checks": {},
    }

    async def stage(name, model, credential, operation):
        """Record one adapter check while keeping raw failures and secrets out of reports."""
        result = {"model": model, "network_request_attempted": False}
        report["checks"][name] = result
        if not credential or not credential.get_secret_value().strip():
            result.update(status="BLOCKED", reason="MISSING_PROVIDER_CREDENTIAL")
            return None
        started = monotonic()
        result["network_request_attempted"] = True
        try:
            async with asyncio.timeout(timeout_s):
                value = await operation()
            result.update(status="PASSED", diagnostics=value[1] if name == "tts" else value)
            return value
        except Exception as exc:
            result.update(status="FAILED", **safe_error(exc))
            return None
        finally:
            result["latency_ms"] = round((monotonic() - started) * 1000, 2)

    tts = await stage(
        "tts",
        settings.tts_model,
        settings.sarvam_api_key,
        lambda: check_tts(settings, output_dir / "synthetic-speech.wav"),
    )
    await stage("llm", settings.llm_model, settings.deepseek_api_key, lambda: check_model(settings))
    if tts:
        await stage(
            "stt", settings.stt_model, settings.sarvam_api_key, lambda: check_stt(settings, tts[0])
        )
    else:
        report["checks"]["stt"] = {
            "model": settings.stt_model,
            "status": "BLOCKED",
            "network_request_attempted": False,
            "reason": "SYNTHETIC_TTS_INPUT_UNAVAILABLE",
        }
    report["passed"] = all(r["status"] == "PASSED" for r in report["checks"].values())
    return report


def main(argv=None):
    """Require --live before reading credentials, then persist/print a sanitized report.

    Returns 0 only when all providers pass, 1 for a failed/blocked check, and
    argparse exits 2 when opt-in or CLI arguments are absent/invalid. Voice
    ingress flags are not changed and Exotel credentials are not required.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow billable provider requests")
    parser.add_argument("--output", type=Path, default=Path("reports/live-provider-smoke"))
    parser.add_argument("--timeout-s", type=float, default=30)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("Pass --live to opt in to actual provider requests; normal CI uses mocks.")
    if not 1 <= args.timeout_s <= 120:
        parser.error("--timeout-s must be between 1 and 120")
    try:
        settings = VoiceSettings().model_copy(update={"provider_timeout_s": args.timeout_s})
        report = asyncio.run(run_checks(settings, args.output, args.timeout_s))
    except Exception as exc:
        report = {"execution": "LIVE", "passed": False, **safe_error(exc)}
    args.output.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    (args.output / "report.json").write_text(serialized + "\n")
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
