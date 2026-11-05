# PR #1: Slice 2 verification and engineering handoff

Date: 5 November 2026. Existing PR: [abhinavtyagiO/pratham-ai#1](https://github.com/abhinavtyagiO/pratham-ai/pull/1).
Branch: `feature/slice-2-realtime-voice`. The PR remains draft and unmerged. No public deployment,
conditional forwarding, or real phone call was performed.

## Code changes

- `media_sample_rate()` normalizes Exotel's two documented metadata representations to mono
  PCM16: `audio/x-raw` with depth 16; `raw` with rate-derived bitrate 128/256/384 kbps at
  8/16/24 kHz. Mismatched units, compressed codecs, stereo, unsupported rates, malformed numeric
  fields, and incomplete descriptions still fail closed. Authentication, stream/call checks,
  event order, payload validation, size limits, and account/destination checks are preserved.
- Contract coverage includes both representations, numeric/string fields, supported rates,
  matching outbound audio, replay/cross-stream rejection, stop lifecycle, and invalid formats.
- Server-VAD integration exercises the actual Saaras event adapter and complete PCM/Pipecat
  session/store path with a fake provider peer. It covers start/end, partial/final text,
  pauses longer than local VAD's threshold, duplicate finals, cancellation during model work,
  playback interruption, late cleared marks, and three turns in one call.
- A newly exposed ACK/interruption race is fixed. Valid mark receipt publishes PLAYED before
  awaiting persistence; interruption rechecks delivery after waiting for the send/clear lock.
  A deterministic regression test holds the ACK database write while new speech arrives.
- `app.live_provider_smoke` is opt-in (`--live`), uses production adapters and synthetic data,
  writes audible WAV plus sanitized diagnostics, and never opens an Exotel call. Missing
  credentials/dependencies are BLOCKED; actual provider failures are FAILED, with no fake-success
  fallback. Its ordinary unit tests use doubles and cannot constitute live verification.
- Normal tests bypass developer `.env` and clear deployment credentials/mode from test settings.
  The default audio harness also ignores `.env`; explicit injected settings remain supported.

The deterministic owner-policy/context/draft/render boundary is preserved. The model still
cannot supply arbitrary speech, prices, inventory, alternate hours, tools, or caller instructions.
No unrelated MVP features were added.

## Documentation delivered

[VOICE_DATA_FLOW.md](VOICE_DATA_FLOW.md) traces all ten call stages to their concrete classes
and methods, with architecture, format reconciliation, concurrency ownership, interruption,
disconnect, provider failure, delivery state, persistence, and cleanup explanations.

[CODEBASE_GUIDE.md](CODEBASE_GUIDE.md) lists every newly documented production module/class:
**293 definitions across 36 modules, including 82 classes**. Coverage includes all first-party
production classes, functions/methods, nested callbacks, protocol methods, migration functions,
and test helper classes/methods. Generated dependency methods are excluded. Docstrings describe
responsibilities, inputs/results, important state, side effects/failures, and async ownership.
Inline comments explain non-obvious audio, policy, invalidation, ACK, and transaction decisions.

The documentation-only edits were checked for unchanged executable AST. A source-level test
keeps definition documentation coverage complete. The operator runbook and both READMEs now link
the current implementation and distinguish simulated, live-provider, and phone evidence.

## Automated results

| Check | Actual local result |
| --- | --- |
| Complete pytest suite | **160 passed, 1 skipped**, 6.31 s |
| Skipped check | PostgreSQL migration + single-turn/voice round trip; `TEST_POSTGRES_URL` absent |
| Golden scenarios | **20/20**, intent/policy 100%, zero unsafe disclosures |
| PCM simulation | **RESOLVED**, two turns, playable 8 kHz mono PCM16 tone WAV |
| Ruff lint | Passed |
| Ruff formatting | Passed, 68 Python files |
| Dependency consistency | Passed (`pip check`) |
| Documentation audit | Passed as part of pytest |

The only suite warning is Pipecat's upstream `audioop` deprecation under Python 3.12. The
container daemon was unreachable locally, so a local PostgreSQL pass is not claimed. PR CI
provides PostgreSQL 17, migration upgrade/downgrade, schema/metadata checks, and durable voice
round-trip verification on an isolated temporary schema. Current-head CI evidence is recorded
in the PR after the push; the earlier Slice 2 CI result is historical, not evidence for this diff.

Local artifacts under `backend/reports/` are ignored by Git. The PCM harness output is explicitly
scripted recognition plus test tones; it is not a Sarvam speech or phone acceptance result.

## Actual provider execution: one approved run

After explicit permission to run **only once**, the utility executed once at
`2026-11-05T16:38:29.161945+00:00`. It exited **0** with all three live checks **PASSED**.
The [sanitized JSON evidence](verification/live-provider-smoke-2026-11-05.json) is retained for
review. No retry or second live run was performed.

| Provider check | Result | Measured evidence |
| --- | --- | --- |
| Bulbul `bulbul:v4-flash` | PASSED | 4.537 s speech, 145200 PCM bytes, first audio 1807.73 ms, total check 2534.29 ms |
| DeepSeek `deepseek-flash` | PASSED | Thinking disabled, schema-valid POLITE draft, 137 input / 15 output tokens, 2079.05 ms |
| Saaras `saaras:v4` | PASSED | Server VAD: 1 speech start, 71 partials, 1 speech end, 1 nonempty final; total paced check 6720.97 ms |

Bulbul synthesized the fixed Hindi test sentence. Its generated speech was streamed at real-time
speed into Saaras, with bracketing silence; STT evidence stores final length/hash rather than raw
provider text. The model received only synthetic authorized context. No customer speech, tokens,
headers, provider error bodies, or account identifiers are in the committed evidence.

The actual audible artifact is `backend/reports/live-provider-smoke/synthetic-speech.wav`
(mono, 16-bit, 16 kHz); it is retained locally and excluded from Git. Audio-level/duration checks
passed. Human assessment of voice quality, pronunciation, phone acoustics, and end-to-end latency
is still required. These one-shot measurements include network/connection cost and, for STT,
paced audio duration; they are not production SLOs or general recognition-accuracy benchmarks.

## Remaining integration work

1. No controlled Exotel telephone call has exercised the actual account's start envelope,
   marks/clear timing, carrier audio, or disconnect semantics.
2. An explicitly approved secure WSS endpoint and direct-number Voicebot routing remain to be
   configured. No public endpoint/tunnel/forwarding was created by this work.
3. Ingress remains disabled/mock in the development configuration. Before the real-phone pilot,
   enable live/server-VAD mode and confirm owner facts, account SID, destination, and ingress secret.
4. A human listening test must assess Hindi/Hinglish, noise/echo, pauses, interruptions, concurrent
   calls, silence close, and hanging up during generation. One synthetic provider run cannot cover these.
5. Single-worker admission, no automatic reconnect, no process-crash reconciliation, bounded
   keyword intent detection, and a restricted response vocabulary remain the intended pilot limits.

## Next real-phone test commands

These are preparation/runbook commands, not an instruction to deploy during this task. From
`backend/`, after the operator has confirmed `.env` values:

```sh
# In backend/.env, set VOICE_ENABLED=true, VOICE_MODE=live, VOICE_VAD_MODE=sarvam.
# Keep actual API/provider/ingress credentials only in that ignored file or a secret store.
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.seed
../.venv/bin/python -m app.voice_preflight
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Follow [LIVE_VOICE_TEST.md](LIVE_VOICE_TEST.md) to place that loopback service behind an approved
TLS WebSocket endpoint and configure the **direct** Exotel number's Voicebot applet with Basic
authentication and `?sample-rate=8000`. Call it manually and record application call/trace IDs,
human listening observations, delivery states, policy decisions, and outcome. Keep conditional
forwarding and PR merge out of this first controlled test.
