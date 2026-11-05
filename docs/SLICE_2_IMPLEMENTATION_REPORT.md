# Pratham AI — Slices 2A and 2B implementation report

5 November 2026. Feature branch: `feature/slice-2-realtime-voice`.

**Review boundary:** real-time infrastructure and live-provider adapters are implemented and
verified with deterministic PCM and mocked provider protocols. **No real provider service or
telephone call was used. Real-phone acceptance is NOT TESTED.**

The earlier checkpoint is preserved in commit `188d243` and [SLICE_2A_REPORT.md](SLICE_2A_REPORT.md).
The supplied request is retained as [SLICE_2_BRIEF.md](SLICE_2_BRIEF.md).

## What is implemented

An authenticated Exotel connection creates an isolated Pipecat pipeline and call trace. Real
PCM audio is buffered/resampled, speech events delimit customer turns, and each final transcript
passes through the existing intent detector, fresh business-context retrieval, and deterministic
owner-policy engine. The response provider receives only the permitted typed context.

DeepSeek Flash runs with thinking explicitly disabled and JSON output enabled. Its SSE output
is buffered until the **complete response passes `ResponseDraft` validation**. The model selects
approved phrasing and whether to include the closing time. A deterministic renderer supplies the
actual speech text; no arbitrary or partial LLM text is sent to TTS. Native Hindi templates improve
the input script for Bulbul while retaining the same authorized meaning as Slice 1.

Sarvam Saaras v4 uses `/speech-to-text-realtime/ws`, not the legacy endpoint. Both provider VAD
and locally controlled energy VAD are supported. Partial transcripts do not trigger a response.
Bulbul v4 Flash uses its WebSocket config/text/flush protocol. WAV headers and audio properties
are validated before raw PCM is sent to Exotel. Each TTS utterance owns its socket, so cancellation
cannot leave old audio queued for a subsequent utterance on a shared provider connection.

Incoming speech cancels an obsolete response, invalidates its generation, drops queued audio,
and sends Exotel `clear` under the same lock as media writes. Marks from a cleared generation
cannot mark it as played. Assistant messages retain explicit delivery states; only an acknowledged
playback mark gives `PLAYED`. Interrupted or unconfirmed turns conservatively need owner review.

Per-call bounded queues, timeouts, silence prompting/close, provider shutdown, database transaction
ordering, partial transcripts, and cancellation diagnostics handle failure paths. A model error
has one authorized deterministic fallback and creates an attention item. STT/TTS failures end
cleanly with available records; automatic reconnect/replay is deliberately absent.

The original synchronous simulation/eval path remains supported. All call-data, simulation, and
metrics HTTP endpoints now require a configured nonempty administrator Bearer key. Exotel
WebSocket ingress uses separate HTTP Basic credentials and, in live mode, account/destination
checks. Missing live credentials fail startup; there is no implicit live-to-mock downgrade.

## Architecture and key files

| Area | Files |
| --- | --- |
| Official Exotel event boundary | `backend/app/realtime/protocol.py` |
| PCM conversion/buffers/VAD | `backend/app/realtime/audio.py` |
| Pipecat processors and worker | `backend/app/realtime/pipeline.py` |
| Session lifecycle, turns, policy orchestration | `backend/app/realtime/session.py` |
| Bounded/paced playback, generation clearing, marks | `backend/app/realtime/playback.py` |
| Short, serialized per-call DB transactions | `backend/app/realtime/store.py` |
| Admission registry and WebSocket route | `backend/app/realtime/router.py` |
| Provider-independent streaming contracts | `backend/app/realtime/contracts.py` |
| Sarvam STT and TTS adapters | `backend/app/providers/sarvam_realtime.py` |
| DeepSeek Flash adapter and JSON gate | `backend/app/providers/deepseek.py` |
| Reviewed native Hindi rendering | `backend/app/realtime/speech_text.py` |
| Streaming RIFF/WAVE validation | `backend/app/realtime/wav.py` |
| Provider selection and live settings validation | `backend/app/realtime/factory.py`, `config.py` |
| API/ingress authentication | `backend/app/api/security.py`, `backend/app/main.py` |
| Real-PCM fixtures and executable harness | `backend/app/realtime/mock.py`, `harness.py`, `backend/app/simulate_audio.py` |
| Secret-free configuration diagnostics | `backend/app/voice_preflight.py` |
| Schema | `backend/app/persistence/models.py`, migration `360a7acf684a` |
| Versioned model contract | `backend/app/prompts/templates.py`, `response_generation:v2` |

The additive migration introduces voice session/turn records, provider call/stream uniqueness,
per-turn policy references, and transcript delivery status. Existing Slice 1 data is preserved.
Final call status, outcome, turn state, and end reason commit together. Database work is offloaded
from the event loop; a cancelled operation waits for its DB transaction before releasing its lock.

No mobile UI, inventory, claims workflow, WhatsApp, live transfer, voice cloning, or new business
intents were introduced. No microservice or background-job infrastructure was added.

## Tests and actual evidence

| Check | Result |
| --- | --- |
| Slice 2A checkpoint suite | **100 passed, 1 skipped** |
| Final local suite | **120 passed, 1 skipped**, 3.61 seconds |
| Skipped locally | PostgreSQL: Docker daemon stopped / `TEST_POSTGRES_URL` absent |
| Original golden evals | **20/20 passed**, zero unsafe disclosures |
| Real-PCM executable harness | Two price turns, `RESOLVED`; valid 8 kHz mono PCM WAV |
| Ruff lint/format | Passed |
| Dependency consistency | `pip check` passed |
| Configuration preflight | Missing service/ingress/API credentials correctly listed; no network calls |
| Actual Sarvam / DeepSeek requests | **Not performed** |
| Real Exotel phone call | **Not performed** |

Generated local evidence is in ignored `backend/reports/`: `slice-2-tests.txt`, `slice-2-eval.json`,
`slice-2-eval.txt`, `audio-simulation-result.json`, `audio-simulation-trace.jsonl`, and
`audio-simulation/{call.json,playback.wav}`. Test counts include the original tests and golden
regressions. One upstream Pipecat `audioop` deprecation warning remains on Python 3.12.

The GitHub workflow preserves lint, formatting, pytest, PostgreSQL, golden evals, and report
uploads, and adds the real-PCM harness. Its PostgreSQL test now exercises a voice session as well
as the original Slice 1 flow. The PR's check results provide the remote-run status separately.

### What the automated tests exercise

- Real PCM samples, resampling duration/amplitude, packet splits, padding, VAD speech minimums,
  and pauses shorter than the configured silence interval.
- Exotel documented event fields/directions, supported sample rates, invalid JSON/base64,
  unsupported codecs, wrong stream IDs, non-monotonic sequence numbers, and events after stop.
- Complete policy-compliant price turns, two turns in one call, and two independent concurrent
  calls with distinct traces and transcripts.
- Speech partials, slow speech, cancellation during TTS/model generation, late stale marks,
  hangup while generating audio, server cancellation, and incomplete playback.
- Model timeout/malformed/injected output, safe fallback, STT connection/read errors, TTS stalls,
  no playback ACK, media/silence limits, bounded queues, and stalled sends.
- Fresh owner rules each turn: disabling the rule between turns fails closed to attention.
- Ordered messages and delivery states, call outcome aggregation, invocation/cancellation
  diagnostics, failure persistence, and raw customer/key text excluded from operational events.
- Authenticated HTTP/WebSocket lifecycle; wrong/missing keys, blank configuration, invalid
  account SID, disabled ingress, duplicate active calls, and capacity limits.
- Actual adapter classes against mocked Sarvam/DeepSeek network messages: realtime STT query
  parameters and audio envelope, Bulbul config/text/flush and fragmented WAV decoding, DeepSeek
  thinking-disabled JSON SSE/usage, invalid/truncated output rejection, and provider socket close
  on cancellation. A complete two-turn test uses all three real adapter implementations with
  injected mocked network responses.

## Limits and external dependencies

1. No Exotel account configuration, live provider credentials, public WSS deployment, or physical
   telephone call is available in this implementation run. Provider subscription/balance/model
   entitlement, exact account behavior, and real-phone quality/latency remain external validation.
2. Pipecat is pinned at 1.15.0. Its generic Exotel serializer was inspected; it lacks the playback
   state accounting needed here and can log malformed inbound JSON verbatim. The application
   supplies its own strict transport boundary while using the real Pipecat frame pipeline.
3. The model's phrasing vocabulary remains intentionally restricted (`POLITE`/`BRIEF`, time flag).
   This is an actual DeepSeek integration, but unrestricted conversational prose and LLM-managed
   dialogue are not enabled. Caller text/history is not sent to the response model. The existing
   keyword classifier still supports only price enquiries and unknown requests.
4. Multi-turn transport/persistence is supported, but pronoun resolution, conversational memory,
   speech confidence handling, broad intent coverage, and a general information-collection agent
   are deferred. Unknown requests are recorded and need owner attention.
5. Energy VAD is a testable baseline, not noise/echo-robust production detection. Live mode can
   use Sarvam VAD; real phone noise, accents, slow speech, echo, and barge-in thresholds need tuning.
6. Only documented PCM16LE mono Exotel media is accepted. Bulbul WAV must be PCM16 mono at the
   requested rate. Mismatches fail safely. Model/voice/rate/codec parameters are explicit because
   Sarvam's current guides disagree about some defaults and optional formats.
7. One TTS socket per utterance simplifies cancellation and adds handshake latency. No latency
   SLO is claimed from the fast, unpaced mock harness. First sent audio and a playback mark are
   distinct observations; neither replaces a human listening test.
8. Authentication is for a single-business pilot administrator and a configured Exotel ingress.
   Multi-tenant identity, retention controls, reconnect routing, and multi-worker admission are
   deferred. Use one worker, TLS termination, and no access/header/body logging for the pilot.
9. Metrics are in-process unless exported to an existing OTLP collector; no live collector was
   tested. Process-kill recovery and stale `IN_PROGRESS` reconciliation remain unimplemented.
10. The original Slice 1 replay command is a single-turn tool. It does not replay full voice
    sessions or original audio; no caller audio is retained by this code.

## Reproduction and next review

```sh
cd backend
make migrate
make seed
make test
make eval
make simulate-audio
../.venv/bin/python -m app.voice_preflight
```

The preflight intentionally exits nonzero until required live configuration is present.
Read [LIVE_VOICE_TEST.md](LIVE_VOICE_TEST.md) for exact credential names, direct Exotel number/flow
setup, secure WSS requirements, VAD tuning, authenticated transcript retrieval, and the actual
phone acceptance procedure. No external services are needed to reproduce the mock tests.

The next acceptance step is an operator-configured direct-number pilot and a documented human
listening test. Conditional mobile-number forwarding follows after that. Implementation stops
at this review boundary.
