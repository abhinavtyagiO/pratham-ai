# Slice 1 implementation report

Date: 3 November 2026. Status: implemented and verified in deterministic simulation; ready for review.
The product boundary and original requirements are retained in [PRODUCT_CONTEXT.md](PRODUCT_CONTEXT.md)
and [PRODUCT_BRIEF.md](PRODUCT_BRIEF.md).

## Delivered behavior

A simulated call greets the customer as an AI assistant, accepts a price question, detects
`PRICE_ENQUIRY`, applies the seeded `DO_NOT_DISCLOSE_PRICE` owner rule, and returns:

> Sir, rate ki pooch-taach ke liye aap shop par aa sakte hain. Shop ka band hone ka samay shaam 6:30 hai.

The complete greeting/customer/reply transcript, call metadata, summary, `RESOLVED` outcome,
policy decision, configuration snapshot, and model diagnostics are persisted. The transcript
and diagnostics are retrievable through the local API. Unknown intent or a missing policy
creates an attention outcome; execution failures preserve available transcript and failure data.

Sample verified call: `1b05be1c-7beb-4a8c-8f00-aaed21284997`.
Trace: `9549b25661e8a5b51abdb0173a19584d`.
Replay call: `35fb848e-d8e3-49a5-b10e-c2c3d9fe8833`.
These records are in the local `backend/pa.db`; generated evidence is in `backend/reports/`.

## Architecture decisions

- One Python/FastAPI modular monolith. Domain types, policy, context/rendering, provider contracts,
  persistence, orchestration, prompts, and telemetry have separate modules.
- SQLAlchemy and an explicit, reversible Alembic migration support PostgreSQL and SQLite.
  SQLite supplies immediate credential-free execution; Compose and CI supply PostgreSQL 17.
- Rules are deterministic and business-scoped. Highest numeric priority wins, with a stable
  rule-ID tie-break. Missing policy fails closed to owner attention.
- Generation uses an explicit context allowlist and a strict response schema. The provider can
  select approved Hindi/Hinglish phrasing and a closing-time flag. The backend renders actual
  speech and inserts known facts. There is no price, inventory, arbitrary output-text, or tool
  capability in the model response contract.
- Free-text owner instructions are stored for review; the structured action defines behavior.
  No natural-language rule interpreter was added.
- Provider interfaces cover telephony, STT, TTS, intent detection, and conversation modeling.
  Only deterministic mock adapters are configured; unsupported provider modes fail explicitly.
- Versioned response and summary invocations retain provider/model, safe context identifiers,
  structured output, duration, token usage when available, error class, and retry count.
- OpenTelemetry spans and JSON events share one call trace. Optional OTLP/HTTP export supports
  an existing collector. Operational logs omit raw caller text, phone numbers, and error messages.
- Messages and decisions are committed incrementally. Final status/outcome commit together.
  Failure-persistence errors are explicitly logged. UTC-aware timestamps work across SQLite and PG.
- Single-call replay uses original business/rule snapshots with current code/providers and creates
  a fresh call/trace. It does not mutate the source call.

## Verification results

| Check | Result |
| --- | --- |
| pytest, including golden regressions | **66 passed, 1 skipped**, in 1.98 s |
| Skipped test | Live PostgreSQL migration/round-trip; `TEST_POSTGRES_URL` not set |
| Independent golden evaluation command | **20/20 scenarios passed** |
| Intent accuracy on this dataset | **100%** |
| Policy adherence on this dataset | **100%** |
| Unsafe disclosures on this dataset | **0** |
| Ruff lint and format | Passed |
| Dependency consistency (`pip check`) | Passed |
| SQLite migration upgrade/downgrade and metadata check | Passed |
| CLI simulation and snapshot replay | Passed |
| HTTP simulate/retrieve/validation/failure tests | Passed |
| GitHub Actions | Workflow supplied; not run remotely |

The suite emitted one upstream Starlette warning about its future test-client migration from
`httpx` to `httpx2`. It did not fail a check.

The deterministic suite tests the business pipeline and constrained output vocabulary. These
percentages do **not** establish real-model quality or live speech/call accuracy.

## Acceptance evidence

| # | Requirement | Evidence |
| --- | --- | --- |
| 1–3 | Seeded tyre business, 18:30 close, owner prohibition rule | `app/seed.py`; seed/idempotency integration test |
| 4–6 | Simulated price query, correct intent and rule | CLI smoke test; `tests/integration/test_calls.py`; golden cases |
| 7 | Pricing unavailable to generation | Allowlisted `ModelContext`; strict `ResponseDraft`; context-leak and malicious-provider tests |
| 8 | Natural Hindi/Hinglish response | Reviewed templates and both phrasing tests; golden behavior checks |
| 9–13 | Conversation, resolved outcome, decision, retrievable transcript, summary | End-to-end repository and HTTP API tests |
| 14–15 | Structured major-stage logs and one complete trace | Event/trace assertions and privacy checks; sample `simulation.jsonl` |
| 16–17 | Unit and integration tests | Passing pytest suite |
| 18 | Golden eval suite | `make eval`, 20/20; `reports/eval.json` |
| 19 | Vendors mockable without credentials | Provider contract tests; mock configuration; injected failure tests |
| 20 | Reproduction instructions | Root and backend README guides, `.env.example`, Makefile |

Additional checks cover disabled/foreign-business rules, priority ties, unknown intent, missing
policy, provider timeouts, response TTS failure, malformed provider output, policy/context errors,
database failure, failure-recording failure, redacted logs, replay, UTC data, and API validation.

## Known limits and remaining validation

1. Live PostgreSQL execution remains unverified locally because the Docker daemon is stopped.
   The opt-in test and PostgreSQL CI service are supplied. SQLite execution is verified.
2. There is no live telephony, audio recognition/synthesis, or external LLM integration. Mock
   STT/TTS transport UTF-8 text bytes; mock TTS does not produce playable audio.
3. Intent detection is a deliberately limited keyword classifier with `PRICE_ENQUIRY`/`UNKNOWN`.
   It can misclassify negation, quoted price terms, mixed requests, or novel phrasing. The small
   golden set is an initial regression dataset, not production evidence.
4. Response and summary generation use approved templates and structured provider choices.
   Arbitrary natural-language generation and descriptive free-text summaries are not implemented.
5. The harness processes one customer turn. It asks for details on uncertainty and saves the
   query, but a multi-turn information-collection dialogue is deferred.
6. There are no contacts, mobile app, claims workflow, inventory, transfers, other verticals,
   or additional business intents. The closing-time fact is not live open/closed determination.
7. The engineering API has no authentication/tenant authorization and runs on loopback. The local
   database contains synthetic data. Pilot deployment, retention controls, and user access are deferred.
8. A process crash can leave an `IN_PROGRESS` call. No recovery worker, automatic retries, batch
   replay, original audio retention, or stale-call reconciliation was added.
9. Metrics reset on restart without an external collector. The optional OTLP path was not tested
   against a live collector. Correlated JSON spans were verified locally.
10. The workspace was initially empty and has no Git repository/remote. No commit, PR, deployment,
    or remote CI run was created.

## Reproduce and inspect

From this workspace's root:

```sh
cd backend
make simulate
make test
make eval
make serve
```

Then retrieve the saved sample transcript:

```sh
curl http://127.0.0.1:8000/calls/1b05be1c-7beb-4a8c-8f00-aaed21284997
```

Replay it independently:

```sh
../.venv/bin/python -m app.simulate_call --replay 1b05be1c-7beb-4a8c-8f00-aaed21284997
```

Fresh-install, PostgreSQL, and trace-export instructions are in [the backend README](../backend/README.md).
No external credentials are needed now. Future live adapters will require the selected telephony,
speech, and model provider credentials; none were requested, stored, or used in this slice.

## Files created

```text
.gitignore
.github/workflows/slice-1.yml
README.md
compose.yaml
docs/
  PRODUCT_BRIEF.md
  PRODUCT_CONTEXT.md
  SLICE_1_REPORT.md
backend/
  .env.example
  README.md
  Makefile
  pyproject.toml
  requirements.lock
  alembic.ini
  app/
    __init__.py
    config.py
    main.py
    seed.py
    simulate_call.py
    evals.py
    domain/{__init__,types,policies,conversations}.py
    providers/{__init__,interfaces,mock}.py
    observability/{__init__,telemetry}.py
    persistence/{__init__,database,models,repository}.py
    prompts/{__init__,templates}.py
    services/{__init__,errors,orchestrator}.py
  migrations/
    env.py
    script.py.mako
    versions/8fc26a4222af_slice_1_business_calls_policies_and_.py
  tests/
    conftest.py
    unit/test_policy.py
    contract/test_mock_providers.py
    integration/test_api.py
    integration/test_calls.py
    integration/test_error_boundaries.py
    integration/test_migrations.py
    evals/golden.json
    evals/test_golden.py
```

Local generated files, excluded by `.gitignore`: `.venv/`, `backend/pa.db`, Python/test/lint caches,
editable-install metadata, and `backend/reports/` (`pytest.txt`, `eval.json`, `eval-trace.jsonl`,
`simulation.json`, `simulation.jsonl`, `replay.json`, `replay.jsonl`).

Slice 1 ends here. Later slices await the next task.
