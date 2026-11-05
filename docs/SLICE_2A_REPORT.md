# Slice 2A checkpoint — 5 November 2026

Implemented on `feature/slice-2-realtime-voice`, extending the accepted Slice 1.

## Delivered

- Authenticated Exotel Voicebot WebSocket endpoint at `/voice/exotel`. HTTP Basic credentials
  are checked before upgrade; optional account and destination checks bind ingress to the pilot.
- Explicit parsing for Exotel's snake_case `connected`, `start`, `media`, `dtmf`, `mark`, and
  `stop` events; outbound PCM `media`, playback `mark`, and barge-in `clear`. Malformed, replayed,
  wrong-stream, and unsupported-format events fail closed. `clear` is outbound only.
- Pipecat 1.15.0 `PipelineWorker`/`WorkerRunner` with custom input, transcription, and authorized
  output processors. No deprecated `PipelineTask` API and no unverified vendor service adapter.
- PCM16LE mono at Exotel's 8/16/24 kHz; stateful resampling to 16 kHz, 20 ms internal frames,
  and output buffering/padding into documented minimum 3,200-byte packets. Real PCM test tones
  replace the Slice 1 UTF-8 speech mocks in this path.
- Per-call providers, pipeline, tasks, database transaction lock, resampler, buffers, turn state,
  playback queue, trace, and cancellation generation. An in-process admission registry limits
  concurrent connections and prevents duplicate active call SIDs. Database uniqueness also
  prevents reusing a provider call/stream ID after process restart.
- Configurable energy VAD, minimum speech length, end-of-turn silence, and maximum utterance
  duration; provider-driven VAD is an interface option for Slice 2B.
- Multi-turn policy evaluation and fresh business/rule retrieval each turn. Existing intent,
  policy, context allowlist, strict `ResponseDraft`, rendering, and diagnostics are reused.
- Generation cancellation and serialized audio clear. Queued stale generations are discarded;
  marks returned after clear cannot confirm a cancelled reply.
- A response is marked `PLAYED` only after the Exotel playback mark is acknowledged. Generated,
  interrupted, and unconfirmed text remain explicitly labeled in the transcript. A send is
  evidence of transport delivery, not proof of human comprehension.
- Bounded input acknowledgements and output queues, send/provider/playback/handshake/media/call
  timeouts, one silence prompt then close, shutdown cleanup, partial transcript preservation,
  and a deterministic authorized fallback for model failure.
- Authenticated Bearer access for all existing call-data, simulation, and metrics HTTP routes.
  Health remains public. An unconfigured API key disables data access.
- Additive migration `360a7acf684a`, `voice_sessions`, `voice_turns`, message delivery status,
  and per-turn policy references. A call's final status, outcome, turn state, and end reason
  are committed together. Database operations run in short thread-offloaded transactions.
- Structured milestones for final STT, intent/policy, first validated model response, first TTS
  audio, first audio sent, cancellation/clear, playback ACK, completion/failure; one call trace.
  Raw text, phones, credentials, media payloads, and provider exception messages are not logged.

## Evidence

- `pytest -q`: **100 passed, 1 skipped**, 3.18 seconds.
- Skipped: live PostgreSQL test (`TEST_POSTGRES_URL` absent; local Docker daemon stopped).
- `python -m app.evals`: **20/20**, 100% intent/policy/scenario scores, zero disclosures on the
  original deterministic dataset.
- Ruff lint/format and `pip check`: passed.
- `python -m app.simulate_audio`: two price turns, `RESOLVED`, ordered transcript, and a valid
  8 kHz mono PCM WAV in `backend/reports/audio-simulation/`.
- Authenticated FastAPI WebSocket handshake, greeting/mark/stop lifecycle tested with TestClient.
- Tests include two simultaneous calls, interruption while TTS is blocked, a late stale ACK,
  hangup during generation, partials and long speech pauses, model timeout/malicious JSON,
  missing playback ACK, playback timeout, silence recovery, and bounded queue/socket stalls.

The upstream Pipecat dependency emits one `audioop` deprecation warning on Python 3.12. No test
is failed or filtered because of it.

## Reproduce

From `backend/`, after installing `requirements.lock` and the editable package:

```sh
make migrate
make seed
make test
make eval
make simulate-audio
```

For the local data API, configure `API_KEY` in the untracked `.env`, then use
`Authorization: Bearer <API_KEY>`. Configure `VOICE_ENABLED=true` and nonempty
`VOICE_EXOTEL_WS_PASSWORD` to enable the mock WebSocket harness. Leave ingress disabled when
not testing. Exotel's flow must use the Voicebot bidirectional applet, not the listen-only Stream
applet. All deployment, provider access, and real-call validation remain external to this checkpoint.

## Official contracts verified

- [Exotel AgentStream protocol](https://developer.exotel.com/docs/agentstream/websocket-protocol)
- [Exotel Voicebot configuration and Basic authentication](https://developer.exotel.com/docs/agentstream/stream-voicebot-applet)
- [Pipecat Exotel integration](https://docs.pipecat.ai/pipecat/telephony/exotel-websockets)
- [Pipecat custom processors](https://docs.pipecat.ai/pipecat/fundamentals/custom-frame-processor)

The installed Pipecat Exotel serializer does not implement the needed playback lifecycle and
logs invalid inbound JSON verbatim. This implementation therefore uses a strict protocol boundary
and application-owned playback accounting, while using Pipecat for the actual frame pipeline.

## Limits

No provider credentials, provider network calls, or real telephone calls were used for this
checkpoint. Mock STT labels VAD-delimited PCM from a fixture script; it does not recognize speech.
Mock TTS outputs audible tones; it does not speak the generated text. Energy VAD is a deterministic
baseline, not a neural/noise-robust detector or echo canceller. Real speech quality, telephone echo,
barge-in sensitivity, carrier behavior, deployment latency, and Exotel account enablement need a
live pilot. Registry capacity is per process; use one worker for this initial pilot. Interrupted
or unconfirmed turns conservatively cause owner attention, even if a later reply was played.

Proceed next to Slice 2B adapters, retaining this verified checkpoint.
