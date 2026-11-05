# Pratham AI backend

Start with the [engineering guide](../docs/CODEBASE_GUIDE.md),
[complete voice data flow](../docs/VOICE_DATA_FLOW.md),
[verification evidence](../docs/SLICE_2_VERIFICATION_REPORT.md), and
[live voice test instructions](../docs/LIVE_VOICE_TEST.md). `make simulate-audio` runs real PCM
through Exotel event parsing, Pipecat, scripted STT, policy/model gating, tone TTS, and playback
acknowledgements without service credentials. Sarvam/DeepSeek live adapters are selected with
`VOICE_MODE=live`; they require credentials. The sections below also retain the Slice 1 harness.

`make smoke-live` explicitly makes actual Sarvam/DeepSeek requests using synthetic input and
writes a speech WAV plus sanitized diagnostics under `reports/live-provider-smoke/`. It is
opt-in, potentially billable, and separate from normal pytest/CI. It never calls Exotel.

FastAPI modular monolith with SQLAlchemy 2, Alembic, Pydantic, pytest, and OpenTelemetry.
The same `ConversationOrchestrator` is used by the CLI, HTTP API, replay, tests, and golden evals.

## Local simulation

All commands below run from `backend/`, using the root `.venv` created in the main README.
Configuration is read from environment variables, then `backend/.env`, then defaults.
SQLite is the credential-free default; PostgreSQL is the intended deployment database.

```sh
make migrate
make seed
../.venv/bin/python -m app.simulate_call "MRF 195/65 R15 ka rate kya hai?"
```

Expected output:

```text
Intent:
PRICE_ENQUIRY

Rule:
DO_NOT_DISCLOSE_PRICE

Response:
Sir, rate ki pooch-taach ke liye aap shop par aa sakte hain. Shop ka band hone ka samay shaam 6:30 hai.

Outcome:
RESOLVED

Call ID:
<UUID>

Trace ID:
<32-character OpenTelemetry trace ID>
```

The greeting explicitly identifies the AI assistant. The closing-time wording describes the
configured hours; it does not claim that the store is currently open. There is no clock-based
opening-hours, holiday, or availability workflow in this slice.

Use `--json` for a machine-readable result. Operational events and completed trace spans are JSON
lines on stderr. Save them separately from the customer-facing output:

```sh
mkdir -p reports
../.venv/bin/python -m app.simulate_call \
  "MRF 195/65 R15 ka rate kya hai?" --json \
  > reports/simulation.json 2> reports/simulation.jsonl
```

The seeded business ID is `00000000-0000-0000-0000-000000000001`. The seed command is idempotent
and does not overwrite existing owner changes. Its store address is explicitly demo data.

## PostgreSQL

Docker Compose is supplied at the repository root. With the Docker daemon running:

```sh
docker compose -f ../compose.yaml up -d --wait
export DATABASE_URL=postgresql+psycopg://pa:pa_local@localhost:5432/pa
make migrate
make seed
make simulate
```

The database binds only to loopback. These credentials belong to local development.
The local machine's Docker daemon was stopped during implementation; the live PostgreSQL test
has not been run locally. CI supplies PostgreSQL 17 and runs the opt-in migration/round-trip test:

```sh
TEST_POSTGRES_URL=postgresql+psycopg://pa:pa_local@localhost:5432/pa make test
```

That test creates and removes a unique `pa_test_<UUID>` schema; the test role must have schema
creation privileges. Normal application tables are not used by the test. SQLite tests and the
eval command use isolated databases and never mutate `backend/pa.db`.

## API

```sh
make serve
```

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Database connectivity |
| POST | `/calls/simulate` | Run one simulated customer turn |
| GET | `/calls/{call_id}` | Metadata, ordered transcript, outcome, decisions, diagnostics, failures |
| GET | `/metrics` | OpenTelemetry SDK metric snapshot as JSON |
| GET | `/docs` | Interactive OpenAPI documentation |

```sh
curl -s http://127.0.0.1:8000/calls/simulate \
  -H "Authorization: Bearer ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"business_id":"00000000-0000-0000-0000-000000000001","utterance":"MRF ka rate kya hai?"}'
curl -s -H "Authorization: Bearer ${API_KEY}" http://127.0.0.1:8000/calls/<CALL_ID>
```

Unknown businesses and calls return 404; invalid input returns 422. Stage failures return 503
with `call_id`, `trace_id`, `stage`, and `category`, without provider exception text.
Failed calls remain retrievable when storage is available.

Set a nonempty `API_KEY` in the untracked `.env` and send `Authorization: Bearer <API_KEY>` to
data routes. Unconfigured keys disable data access; missing/wrong credentials return 401.
This is a single-business pilot administrator credential, not a multi-tenant identity system.
Keep the local server on loopback; public TLS/WebSocket setup is described in the live-test guide.
No deployment or real-phone test has been performed by this implementation.

## Policy and model boundary

1. The intent detector supports `PRICE_ENQUIRY` and `UNKNOWN` only.
2. `PolicyEngine` selects enabled rules for the exact business and intent. Larger `priority`
   wins; equal priority uses the lexicographically smallest rule ID.
3. A missing price rule produces `REQUIRE_OWNER_ATTENTION`; it never implies permission to quote.
4. `build_model_context` constructs a fresh typed allowlist containing only the action and,
   when permitted, the formatted closing time. It does not pass the caller utterance,
   business address, free-text rule instruction, or an arbitrary business payload to generation.
5. The response provider returns `ResponseDraft`: `POLITE`/`BRIEF` and a closing-time flag.
   Extra fields and arbitrary text are rejected. The backend renders the approved phrase and
   inserts the known time. The model cannot supply a price, stock claim, or alternate time.
6. The summary provider acknowledges a structured result; the backend renders a factual summary.
7. Price enquiries with the prohibition rule are `RESOLVED`. Unknown queries and missing rules
   are `NEEDS_ATTENTION`. Failed stages produce a `FAILED` call and an attention outcome.

`BusinessRule.instruction` is retained for owner/reviewer visibility. In slice 1, executable
behavior is determined by the structured action; arbitrary natural-language instructions are
not interpreted. Prices and inventory are not modeled. This deliberately small output contract
must be reviewed when more response capabilities are introduced.

Prompts and speech templates are centralized in `app/prompts/templates.py`. Every invocation
records its version: `response_generation:v1` or `call_summary:v1`. Call snapshots also record
the classifier, policy, and greeting versions.

## Observability and failure behavior

One `incoming_call` OpenTelemetry span owns the entire pipeline, including caller identification,
context loading, speech simulation, intent, policy, response construction, persistence, and summary.
All call events carry `call_id` and `trace_id`. Span JSON includes parent/span IDs and duration.
Every stage exception marks the span as failed; failure records contain category, stage,
provider, exception class, and retryability. No automatic retries are performed in slice 1.

Model invocation records in the application database contain provider/model, IDs, version,
context identifiers, validated structured output, latency, nullable token counts, error class,
and retry count. Mock token counts remain `null`, rather than pretending a model consumed tokens.

Operational logs contain identifiers, bounded decisions, timings, lengths, and output hashes.
They omit raw utterances, caller phone numbers, model input, exception messages, and stack traces.
Transcripts and snapshots remain in the application database and are returned by the local API.

Messages and decisions are committed incrementally so a later provider failure preserves the
partial conversation. An assistant reply is persisted only after simulated TTS succeeds.
The final outcome and call completion are committed together. A provider failure never emits
`call_completed`. If recording the failure also fails, a correlated `failure_persistence_failed`
event is emitted and the CLI/API still reports failure. A process crash can leave `IN_PROGRESS`
records; stale-call reconciliation is not implemented yet.

Counters cover started/completed/failed calls, stage/database errors, intent distribution,
unknown intents, model failures/tokens, resolutions, attention, and triggered rules. Histograms
cover STT, intent, policy, model, TTS, and end-to-end latency. Metric labels use bounded values;
call IDs and phone numbers are not metric labels. `/metrics` is an OTel JSON snapshot, not a
Prometheus endpoint. Without a collector, metrics are in-process and reset on restart.

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` to export standard OTLP/HTTP traces and
metrics to an existing collector. The JSON span exporter remains active. A collector is optional
and is not deployed by this project. Instrumentation follows the
[OpenTelemetry Python SDK](https://opentelemetry.io/docs/languages/python/instrumentation/).

## Tests and golden evals

```sh
make lint
make test
make eval
```

The 20-case golden dataset lives in `tests/evals/golden.json`. Evals run the same providers and
orchestrator against a migrated temporary database. They assess intent, selected policy, outcome,
store invitation/details collection, absence of prices and stock claims, and closing-time
consistency. The command exits nonzero if any scenario fails and writes `reports/eval.json`.

These are deterministic contract/regression checks for the limited response vocabulary. They
are not a semantic judge of unrestricted prose or a real voice/model benchmark. Use
`--dataset PATH --report PATH` to run another structured dataset. New failures should add cases.

GitHub Actions runs lint/format checks, pytest with PostgreSQL, and evals, then uploads the
evaluation report. That workflow is supplied but has not been executed remotely.

## Replay

```sh
../.venv/bin/python -m app.simulate_call --replay <CALL_ID>
```

Replay creates a new call and trace, links `replay_of_call_id`, and uses the original customer
utterance plus the saved business/rule snapshot. Current provider/classifier/policy code and
prompt versions run against those historical facts; the source call remains unchanged. Provider,
classifier, and policy objects can be replaced through the orchestrator's interfaces for tests
and future experiments. This is single-call replay, not a batch evaluation platform.

Calls that failed before a customer utterance was transcribed cannot be replayed through this
command. No original audio is stored in slice 1.

## Provider scope

`TelephonyProvider`, `SpeechToTextProvider`, `TextToSpeechProvider`, `IntentDetector`, and
`ConversationModelProvider` are interfaces. Their mock implementations use UTF-8 bytes for speech
transport and deterministic phrasing choices. Mock TTS does not produce playable audio.
`PROVIDER_MODE=mock` is the only implemented configuration; other values fail explicitly.

The realtime path now has Exotel transport, Sarvam realtime STT/streaming TTS, and DeepSeek Flash
adapters. `PROVIDER_MODE` still selects the original Slice 1 synchronous harness;
`VOICE_MODE` separately selects the audio runtime. These paths do not exchange UTF-8 fake audio.
The mobile app, contacts, claims, inventory, transfers, and other intents remain deferred.
