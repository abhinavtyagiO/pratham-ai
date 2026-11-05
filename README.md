# AI Business PA

Pratham AI includes Exotel PCM transport, Pipecat, multi-turn sessions, interruption/playback
accounting, and policy-gated Sarvam/DeepSeek adapters. Start with the
[engineering guide](docs/CODEBASE_GUIDE.md), [complete voice flow](docs/VOICE_DATA_FLOW.md), and
[Slice 2 verification evidence](docs/SLICE_2_VERIFICATION_REPORT.md). A separate opt-in smoke
utility checks actual providers; the deterministic `make simulate-audio` harness needs no keys.
HTTP data routes require `API_KEY` and Bearer authentication. Historical Slice 1 reports remain.

An owner-controlled personal assistant for incoming customer calls at an Indian tyre retailer.
The owner rule is the authority: **owner rules → known business facts → constrained generation**.

Slice 1 implements a credential-free simulation:

> “MRF 195/65 R15 ka rate kya hai?” → `PRICE_ENQUIRY` → `DO_NOT_DISCLOSE_PRICE`
> → a Hindi/Hinglish store-visit response → transcript, summary, policy decision, and `RESOLVED`.

The simulation and tests have run locally. See [the implementation report](docs/SLICE_1_REPORT.md)
for acceptance evidence, limitations, and the complete file inventory.

## Run in this workspace

The local `.venv` is already installed and `backend/pa.db` is migrated and seeded:

```sh
cd backend
make simulate
make test
make eval
make serve
```

`make serve` listens on `127.0.0.1:8000`; API documentation is at
[localhost:8000/docs](http://127.0.0.1:8000/docs).

## Fresh setup

Requires Python 3.11+; Python 3.12 is used for local verification and CI. From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e ./backend
cd backend
cp .env.example .env
make migrate
make seed
make simulate
```

The [backend guide](backend/README.md) explains PostgreSQL, API usage, diagnostics, and replay.
No telephony, speech, or model API keys are required for deterministic simulations/evaluations.

## Review the logic

- [Policy engine](backend/app/domain/policies.py): enabled rules for the business and intent;
  highest priority wins, then rule ID breaks ties. Missing policy and unknown intent need attention.
- [Context and response boundary](backend/app/domain/conversations.py): an explicit allowlist and
  structured phrasing choices keep prices, arbitrary instructions, and unsupported facts out.
- [Orchestrator](backend/app/services/orchestrator.py): call lifecycle and stage boundaries.
- [Provider contracts](backend/app/providers/interfaces.py): vendor-independent interfaces.
- [Golden conversations](backend/tests/evals/golden.json): 20 Hindi, Hinglish, English,
  adversarial, and unknown-query scenarios.

This slice uses deterministic mocks, a keyword intent classifier, and approved response/summary
templates. Its evaluation results establish local pipeline behavior; they do not measure live
speech recognition, general LLM quality, or production call handling.

The live voice adapters have passed a synthetic provider smoke run. Real Exotel phone acceptance
is still pending; no public deployment, forwarding configuration, or mobile application is included.
