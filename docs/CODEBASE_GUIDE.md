# Pratham AI engineering guide

Start with [the complete voice data flow](VOICE_DATA_FLOW.md), which maps the ten call stages to
their concrete classes/methods and explains interruption/failure cleanup. The
[verification report](SLICE_2_VERIFICATION_REPORT.md) separates automated, live-provider, and
real-phone evidence. [LIVE_VOICE_TEST.md](LIVE_VOICE_TEST.md) is the operator runbook.

## Where to make changes

| Concern | Primary implementation | Important invariant |
| --- | --- | --- |
| Composition/config | `app/main.py`, `config.py`, `realtime/config.py` | Live mode and ingress must be explicitly enabled; secrets stay outside Git |
| Authentication/admission | `api/security.py`, `realtime/router.py` | Authenticate and validate account/destination before constructing providers |
| Carrier transport | `realtime/protocol.py` | Normalize only explicit supported PCM metadata; reject ambiguous/compressed audio |
| PCM and VAD | `realtime/audio.py`, `realtime/wav.py` | Continuous resampler history, whole samples, one turn detector per mode |
| Pipecat | `realtime/pipeline.py` | Acknowledge application frames to bound outstanding work |
| Turn/lifecycle control | `realtime/session.py` | Only nonduplicate finals trigger policy/model work; invalidate generation before awaits |
| Playback | `realtime/playback.py` | Serialize clear/media; only valid marks confirm delivery |
| Provider requests | `providers/sarvam_realtime.py`, `providers/deepseek.py` | Close owned resources on cancellation and validate complete outputs |
| Authorization/rendering | `domain/`, `prompts/templates.py`, `realtime/speech_text.py` | Policy decides capabilities; models select reviewed wording only |
| Persistence | `persistence/`, `realtime/store.py`, `migrations/` | No concurrent Session sharing; ordered short transactions preserve partial evidence |
| Diagnostics | `observability/telemetry.py` | Safe identifiers/types/counts/hashes; no raw speech or provider error strings |
| Simulations/evals | `simulate_call.py`, `simulate_audio.py`, `evals.py`, `realtime/harness.py` | Label mocks accurately and isolate test databases |
| Actual provider smoke | `live_provider_smoke.py` | Explicit `--live`, synthetic data, finite checks, no Exotel call |

## Resource ownership and reading order

Read `router.exotel`, `RealtimeSession.run`, `speech`, `respond`, and `interrupt` first.
Then follow `VoicePipeline.submit`, `SarvamRealtimeSTT.events`, `Playback.run/clear`, and
`VoiceStore.execute/finish`. These methods define ownership and the central state transitions.

Application lifespan owns the shared engine/session factory, telemetry, and process registry.
Each call owns fresh providers, protocol state, pipeline worker, response task, buffers,
generation counters, and playback queue. Each TTS utterance owns its socket/decoder/resampler.
Each database operation owns its thread-local Session. No caller text, model client, send queue,
or conversation state is shared between simultaneous calls.

Docstrings describe responsibilities, parameters/contracts, state, errors, side effects, and
async cleanup where relevant. Inline comments explain the less obvious policy/audio/concurrency
decisions. The documentation pass was checked with executable AST comparison: only documentation
changed in those edits; behavioral corrections are confined to protocol normalization,
ACK/interruption handling, harness synchronization, and explicit test/config isolation.

`tests/unit/test_documentation.py` audits every production definition, migration function, and
first-party test helper class/method for a docstring. It does not import providers or require
credentials. This catches omissions; human review is still responsible for documentation quality.

## Verification commands

From `backend/`:

```sh
../.venv/bin/ruff check .
../.venv/bin/ruff format --check .
../.venv/bin/pytest -q
../.venv/bin/python -m app.evals --report reports/eval.json
../.venv/bin/python -m app.simulate_audio
```

Set `TEST_POSTGRES_URL` for the PostgreSQL 17 migration + single-turn/voice round-trip test. It
creates/drops only a unique test schema. CI supplies a PostgreSQL service and runs this test.
Ordinary pytest ignores development `.env` credentials/mode and injects fake transports.

An actual provider run is explicitly separate and potentially billable:

```sh
../.venv/bin/python -m app.live_provider_smoke --live
```

It writes `reports/live-provider-smoke/report.json` and `synthetic-speech.wav`. These local
artifacts are ignored by Git. Read their actual status before claiming live success; fake unit
tests of the utility do not constitute live provider verification.

## Documented production module/class inventory

Every function/method in the modules below has a docstring, including nested callbacks and
protocol methods. Classes with generated dataclass/Pydantic/ORM methods are documented at their
source definitions; generated third-party methods are not first-party implementations.

| Module | First-party classes | Documented definitions |
| --- | --- | --- |
| [api/security.py](../backend/app/api/security.py) | Functions / CLI | 2 |
| [config.py](../backend/app/config.py) | `Settings` | 1 |
| [domain/conversations.py](../backend/app/domain/conversations.py) | `ModelContext`, `PricePhrasing`, `ResponseDraft`, `SummaryContext`, `SummaryDraft` | 9 |
| [domain/policies.py](../backend/app/domain/policies.py) | `PolicyEngine` | 2 |
| [domain/types.py](../backend/app/domain/types.py) | `Intent`, `Action`, `Outcome`, `CallStatus`, `Speaker`, `BusinessContext`, `OwnerRule`, `IntentResult`, `PolicyResult` | 11 |
| [evals.py](../backend/app/evals.py) | Functions / CLI | 3 |
| [live_provider_smoke.py](../backend/app/live_provider_smoke.py) | Functions / CLI | 9 |
| [main.py](../backend/app/main.py) | `SimulateRequest` | 8 |
| [observability/telemetry.py](../backend/app/observability/telemetry.py) | `JSONFormatter`, `JSONSpanExporter`, `Telemetry` | 12 |
| [persistence/database.py](../backend/app/persistence/database.py) | Functions / CLI | 3 |
| [persistence/models.py](../backend/app/persistence/models.py) | `Base`, `UTCDateTime`, `Business`, `BusinessRule`, `Call`, `ConversationMessage`, `CallOutcome`, `PolicyDecision`, `ModelInvocation`, `CallFailure`, `VoiceSession`, `VoiceTurn` | 16 |
| [persistence/repository.py](../backend/app/persistence/repository.py) | `CallRepository` | 14 |
| [providers/deepseek.py](../backend/app/providers/deepseek.py) | `DeepSeekFlash` | 4 |
| [providers/interfaces.py](../backend/app/providers/interfaces.py) | `ModelResult`, `TelephonyProvider`, `SpeechToTextProvider`, `TextToSpeechProvider`, `IntentDetector`, `ConversationModelProvider`, `Providers` | 13 |
| [providers/mock.py](../backend/app/providers/mock.py) | `MockTelephonyProvider`, `MockSpeechToTextProvider`, `MockTextToSpeechProvider`, `DeterministicIntentDetector`, `MockConversationModelProvider` | 12 |
| [providers/sarvam_realtime.py](../backend/app/providers/sarvam_realtime.py) | `SarvamRealtimeSTT`, `SarvamStreamingTTS` | 14 |
| [realtime/audio.py](../backend/app/realtime/audio.py) | `PCMResampler`, `PCMBuffer`, `EnergyVAD` | 10 |
| [realtime/config.py](../backend/app/realtime/config.py) | `VoiceSettings` | 2 |
| [realtime/contracts.py](../backend/app/realtime/contracts.py) | `SpeechEvent`, `StreamingSTT`, `AsyncResponseModel`, `StreamingTTS`, `VoiceProviders`, `VoiceFailure` | 16 |
| [realtime/factory.py](../backend/app/realtime/factory.py) | Functions / CLI | 2 |
| [realtime/harness.py](../backend/app/realtime/harness.py) | `MemorySocket`, `AudioHarness` | 15 |
| [realtime/mock.py](../backend/app/realtime/mock.py) | `ScriptedPCMSTT`, `MockAsyncModel`, `ToneTTS` | 14 |
| [realtime/pipeline.py](../backend/app/realtime/pipeline.py) | `IncomingPCM`, `RecognizedSpeech`, `AuthorizedPlayback`, `VoiceProcessor`, `VoicePipeline` | 15 |
| [realtime/playback.py](../backend/app/realtime/playback.py) | `Packet`, `Playback` | 9 |
| [realtime/protocol.py](../backend/app/realtime/protocol.py) | `ProtocolError`, `StartInfo`, `ExotelProtocol` | 10 |
| [realtime/router.py](../backend/app/realtime/router.py) | `SessionRegistry` | 5 |
| [realtime/session.py](../backend/app/realtime/session.py) | `RealtimeSession` | 17 |
| [realtime/speech_text.py](../backend/app/realtime/speech_text.py) | Functions / CLI | 1 |
| [realtime/store.py](../backend/app/realtime/store.py) | `VoiceStore` | 21 |
| [realtime/wav.py](../backend/app/realtime/wav.py) | `StreamingWAV` | 4 |
| [seed.py](../backend/app/seed.py) | Functions / CLI | 2 |
| [services/errors.py](../backend/app/services/errors.py) | `ErrorCategory`, `CallExecutionError`, `BusinessNotFound` | 4 |
| [services/orchestrator.py](../backend/app/services/orchestrator.py) | `SimulationResult`, `ConversationOrchestrator` | 9 |
| [simulate_audio.py](../backend/app/simulate_audio.py) | Functions / CLI | 2 |
| [simulate_call.py](../backend/app/simulate_call.py) | Functions / CLI | 1 |
| [voice_preflight.py](../backend/app/voice_preflight.py) | Functions / CLI | 1 |

Coverage: **293 production definitions across 36 modules**, including **82 classes**.
Migration functions and test helper classes/methods are documented and audited separately.
