# AI Business PA — MVP Build Brief

You are building an MVP for an AI Personal Assistant for Indian SMB owners.

Do not try to build the entire product at once.

We will use a **thin vertical slice methodology**. Every slice must work end-to-end, have automated tests, structured logging, traces, and clear acceptance criteria before moving to the next slice.

The initial customer is an MRF / tyre retail store.

---

# PRODUCT PRINCIPLE

The PA represents the business owner on incoming customer calls.

It knows:

- the business;
- selected customers;
- owner-defined rules;
- basic product/business terminology.

Decision priority:

1. Owner rules
2. Known business information
3. AI reasoning within those constraints

The LLM must never override an owner rule.

For uncertain situations:

- do not hallucinate;
- do not invent business information;
- collect useful information;
- mark the call as requiring owner attention.

---

# MVP TECHNOLOGY DIRECTION

Use a modular monolith initially.

Preferred backend:

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Alembic
- Pydantic
- pytest

Use OpenTelemetry-compatible instrumentation.

Do not add Kubernetes, Kafka, microservices, Redis, Celery, or other infrastructure unless the current slice actually requires it.

Mobile application comes after the first voice vertical slice works.

Preferred mobile stack later:

- React Native
- Expo where practical
- TypeScript

---

# SYSTEM BOUNDARIES

Business logic must remain independent of telephony/voice vendors.

Define provider abstractions such as:

```python
class TelephonyProvider:
    ...

class SpeechToTextProvider:
    ...

class TextToSpeechProvider:
    ...

class ConversationModelProvider:
    ...
```

Provider-specific implementations must sit behind those interfaces.

We will likely evaluate:

- Exotel for Indian telephony
- ElevenLabs
- Sarvam

Do NOT tightly couple policy/business logic to any of them.

---

# FIRST VERTICAL SLICE

Build only this:

## Scenario

A customer calls the tyre store.

The PA greets the customer.

Customer asks:

"MRF 195/65 R15 ka rate kya hai?"

The owner has configured:

> Never disclose product prices over phone. Invite the caller to visit the store. Store closes at 6:30 PM.

The system must identify the request as a price enquiry.

The policy engine must return:

`DO_NOT_DISCLOSE_PRICE`

The model must NOT have permission to provide pricing.

The PA should respond naturally with something similar to:

> "Sir rate ki pooch-taach aap shop par aakar kar sakte hain. Shop shaam 6:30 tak khuli hai."

The exact wording may vary.

After the call:

- call metadata is stored;
- transcript is stored;
- summary is generated;
- outcome = RESOLVED;
- applied policy is recorded;
- trace/log information makes the entire execution reconstructable.

---

# IMPORTANT

The first implementation does NOT need a real phone line immediately.

Build the same flow so it can first be exercised through a deterministic simulation/test harness.

Example:

```text
simulate-call
↓
customer utterance
↓
intent detection
↓
policy evaluation
↓
context selection
↓
response generation
↓
transcript
↓
summary
↓
database
```

The business pipeline should work independently of telephony.

Once this passes reliably, attach a real telephony/voice adapter.

---

# DOMAIN MODEL FOR SLICE 1

Implement only what is required.

## Business

Fields:

- id
- name
- business_type
- address
- opening_time
- closing_time
- timezone
- default_language

## BusinessRule

Fields:

- id
- business_id
- intent
- action
- instruction
- enabled
- priority

Initial intent:

`PRICE_ENQUIRY`

Initial action:

`DO_NOT_DISCLOSE_PRICE`

## Call

Fields:

- id
- business_id
- caller_phone
- status
- started_at
- ended_at
- trace_id

## ConversationMessage

Fields:

- id
- call_id
- speaker
- text
- created_at

Speaker values:

- CUSTOMER
- ASSISTANT

## CallOutcome

Fields:

- call_id
- outcome
- summary
- needs_attention
- attention_reason

## PolicyDecision

Persist:

- call_id
- rule_id
- detected_intent
- decision
- decision_timestamp

This is important for debugging.

---

# POLICY ENGINE

Do not implement business rules exclusively through prompt instructions.

Create an explicit policy layer.

For example:

```python
decision = policy_engine.evaluate(
    business_id=business_id,
    intent="PRICE_ENQUIRY",
    caller_context=caller_context
)
```

Possible response:

```json
{
  "decision": "DO_NOT_DISCLOSE_PRICE",
  "rule_id": "...",
  "allowed_context": {
    "closing_time": "18:30"
  },
  "instruction": "Politely invite the caller to visit the store."
}
```

The model should then receive only the information necessary to construct a response.

Do not expose restricted data to the model and merely ask it not to use it.

---

# INTENT DETECTION

For Slice 1 only support:

`PRICE_ENQUIRY`

and

`UNKNOWN`

Keep the interface extensible.

Example:

```python
class Intent:
    PRICE_ENQUIRY = "PRICE_ENQUIRY"
    UNKNOWN = "UNKNOWN"
```

Do not prematurely create dozens of intents.

---

# OBSERVABILITY

Observability is mandatory.

Every call receives:

- `call_id`
- `trace_id`

Every log related to that call must contain them.

Use structured JSON logging.

Example event:

```json
{
  "event": "policy_evaluated",
  "call_id": "...",
  "trace_id": "...",
  "intent": "PRICE_ENQUIRY",
  "rule_id": "...",
  "decision": "DO_NOT_DISCLOSE_PRICE",
  "latency_ms": 4
}
```

Do not rely on free-form console logs.

---

# TRACE THE COMPLETE PIPELINE

Create trace spans approximately like:

```text
incoming_call
│
├── identify_caller
│
├── speech_to_text
│
├── detect_intent
│
├── load_business_context
│
├── evaluate_policy
│
├── build_model_context
│
├── generate_response
│
├── text_to_speech
│
├── persist_conversation
│
└── generate_call_summary
```

Even when some stages are simulated in Slice 1, preserve this conceptual trace structure.

---

# IMPORTANT LOG EVENTS

At minimum emit events for:

- call_started
- caller_identified
- stt_started
- stt_completed
- stt_failed
- intent_detected
- intent_detection_failed
- business_context_loaded
- policy_evaluated
- policy_missing
- model_request_started
- model_request_completed
- model_request_failed
- tts_started
- tts_completed
- tts_failed
- message_persisted
- call_completed
- summary_generated
- call_marked_needs_attention

---

# METRICS

Structure instrumentation so we can later measure:

## System

- calls_started_total
- calls_completed_total
- calls_failed_total
- stage_errors_total
- database_errors_total

## Latency

- stt_latency_ms
- intent_latency_ms
- policy_latency_ms
- llm_latency_ms
- tts_latency_ms
- end_to_end_turn_latency_ms

## AI

- detected_intent
- intent_confidence if available
- model name
- token usage
- model latency
- model failure count

## Product

- resolved_calls
- needs_attention_calls
- rules_triggered_total
- unknown_intent_total

Design these now even if not all dashboards exist yet.

---

# MODEL OBSERVABILITY

For every model invocation persist or log a safe diagnostic record containing:

- provider;
- model;
- call_id;
- trace_id;
- prompt/template version;
- input context identifiers;
- output;
- latency;
- token usage where available;
- error;
- retry count.

Do NOT indiscriminately dump private customer data into infrastructure logs.

Application database transcripts and operational logs are different concepts.

Operational logs should redact sensitive information where practical.

---

# PROMPT VERSIONING

Prompts must not exist as random string literals throughout the codebase.

Give every prompt/template a version.

Example:

```text
intent_detection:v1
response_generation:v1
call_summary:v1
```

Record the prompt version used with every AI invocation.

This allows us to later answer:

> "Did failures start after prompt v4?"

---

# FAILURE CLASSIFICATION

Every failure should be classifiable.

Create error categories such as:

```text
TELEPHONY_ERROR
STT_ERROR
INTENT_ERROR
POLICY_ERROR
CONTEXT_ERROR
MODEL_ERROR
TTS_ERROR
DATABASE_ERROR
UNKNOWN_ERROR
```

When something fails, record:

- stage;
- category;
- provider if relevant;
- error type;
- retryability;
- trace_id.

Avoid swallowing exceptions.

---

# TESTING STRATEGY

Testing should grow beside the product.

Do not wait until MVP completion.

## 1. Unit tests

Test deterministic business logic heavily.

Especially:

- policy evaluation;
- rule priority;
- business-hours formatting;
- context filtering;
- outcome classification.

Example critical assertion:

Given:

```text
intent = PRICE_ENQUIRY
rule = DO_NOT_DISCLOSE_PRICE
```

the resulting model context must never contain a product price.

---

# 2. Integration tests

Test:

```text
utterance
→ intent
→ rule
→ response constraints
→ persistence
```

Use mocked external providers.

---

# 3. Contract tests

Each external provider adapter must have tests ensuring our adapter follows our internal interface.

Provider changes should not leak into the domain layer.

---

# 4. Conversation regression tests

Create a test dataset.

Start small.

Examples:

```text
"MRF ka rate kya hai?"
"195 65 R15 kitne ka padega?"
"Creta wala MRF tyre ka kya price hai?"
"Bhaiya rate bata do"
"price kya chal raha hai?"
```

Expected:

`PRICE_ENQUIRY`

Expected policy:

`DO_NOT_DISCLOSE_PRICE`

Responses do not need exact string matching.

Instead assert semantic constraints:

- does not disclose price;
- invites store visit;
- does not hallucinate availability;
- closing time is correct when mentioned.

---

# 5. Golden conversation dataset

Create:

```text
tests/evals/
```

Store representative conversations as structured fixtures.

Example:

```json
{
  "id": "price_hinglish_001",
  "utterance": "Bhai MRF 195 65 R15 kya rate lagaoge?",
  "expected_intent": "PRICE_ENQUIRY",
  "expected_rule": "DO_NOT_DISCLOSE_PRICE",
  "must_not_contain": ["₹", "Rs", "rupees"],
  "expected_behavior": ["invite_store_visit"]
}
```

This dataset should grow whenever we discover a production failure.

Every real bug should ideally become a regression test.

---

# 6. Model evaluation harness

Build a lightweight command such as:

```bash
make eval
```

or

```bash
python -m evals.run
```

It should run the golden dataset against the configured model and generate a report:

```text
Intent accuracy:      94%
Policy adherence:    100%
Unsafe disclosure:     0
Scenario pass rate:   92%
```

Do not require manual phone calls to evaluate every model change.

---

# 7. Failure replay

Design call storage so a failed conversation can later be replayed against:

- a new prompt;
- a new model;
- a new intent classifier;
- a new policy implementation.

This is critical.

We should eventually be able to say:

> Replay last week's 37 failed calls against response_generation:v5.

---

# 8. External provider mocks

Local development and CI must not require:

- Exotel credentials;
- ElevenLabs credentials;
- Sarvam credentials.

Create deterministic mock providers.

Real integrations should be opt-in through environment configuration.

---

# LOCAL DEVELOPMENT

Provide:

- `.env.example`
- Docker Compose for PostgreSQL if useful
- database migrations
- seed command
- test command
- eval command
- simulation command

A new engineer should be able to run something approximately like:

```bash
docker compose up -d
alembic upgrade head
python -m app.seed
python -m app.simulate_call \
  "MRF 195/65 R15 ka rate kya hai?"
```

Expected output should show:

```text
Intent:
PRICE_ENQUIRY

Rule:
DO_NOT_DISCLOSE_PRICE

Response:
Sir rate ki pooch-taach...

Outcome:
RESOLVED

Call ID:
...

Trace ID:
...
```

---

# REPOSITORY STRUCTURE

Prefer something understandable such as:

```text
backend/
  app/
    api/
    domain/
      businesses/
      calls/
      contacts/
      policies/
      conversations/
    providers/
      telephony/
      stt/
      tts/
      llm/
    observability/
    prompts/
    persistence/
    services/

  tests/
    unit/
    integration/
    contract/
    evals/

  migrations/

  README.md
```

Do not create abstractions with no current use.

---

# SLICE 1 ACCEPTANCE CRITERIA

Slice 1 is complete only when all of the following work:

1. A seeded tyre business exists.
2. Business closes at 18:30.
3. A `DO_NOT_DISCLOSE_PRICE` owner rule exists.
4. A simulated customer can ask a price question.
5. Intent is classified as `PRICE_ENQUIRY`.
6. Policy engine selects the correct rule.
7. Restricted pricing data is not made available to response generation.
8. A natural Hindi/Hinglish response is generated.
9. Conversation is persisted.
10. Outcome is persisted as `RESOLVED`.
11. Policy decision is persisted.
12. Transcript is retrievable.
13. Summary is generated.
14. Every major stage produces structured logs.
15. One trace connects the entire call.
16. Unit tests pass.
17. Integration tests pass.
18. Golden eval suite passes.
19. External vendors can be mocked.
20. README explains how to reproduce the scenario locally.

---

# AFTER SLICE 1

STOP.

Do not automatically continue building features.

Report:

- what was implemented;
- architecture decisions;
- test results;
- eval results;
- known limitations;
- files created;
- how to run the simulation;
- anything that needs credentials.

Then wait for the next slice.

Do not implement the mobile app yet.

Do not implement claims yet.

Do not implement contact importing yet.

Do not implement actual inventory.

Do not implement call transfer.

Do not implement multiple verticals.

Do not add features merely because they seem useful.

The goal of Slice 1 is to prove:

**customer request → owner policy → constrained AI response → persisted outcome → observable execution**

with high confidence.