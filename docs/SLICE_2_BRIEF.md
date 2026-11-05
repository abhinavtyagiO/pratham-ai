Let's resume implementation of **Pratham AI**, building on the accepted Slice 1 in our existing repository:

https://github.com/abhinavtyagiO/pratham-ai

We have finalized the following architecture:

- **Telephony:** Exotel, using bidirectional audio streaming.
- **Voice runtime:** Pipecat integrated with our existing FastAPI backend.
- **Speech recognition:** Sarvam streaming STT (`saaras:v4`), with configurable voice activity detection.
- **LLM:** DeepSeek Flash, preferably non-thinking mode for low latency.
- **Speech synthesis:** Sarvam Bulbul v4 Flash.
- **Business logic:** Our existing deterministic owner-policy engine remains authoritative.
- **Call routing:** Initially test with a direct Exotel number. Later validate conditional forwarding from the owner's existing personal/business mobile number.
- **Voice behavior:** Streaming, multi-turn conversations, configurable turn detection, barge-in, cancellation, silence handling and failure recovery.

**I want you to implement the next meaningful portion of the MVP, not just produce another architecture proposal.**

## Milestone 1 — Slice 2A: Real-time call infrastructure

Extend the existing backend to support real-time telephone sessions.

Implement:

1. Exotel bidirectional media transport, using its actual documented WebSocket protocol.
2. Pipecat-based audio pipeline and session orchestration.
3. Per-call session isolation, lifecycle management and concurrent-call safety.
4. Correct handling of call start, media, mark, clear and stop events, as supported by Exotel.
5. Audio buffering, codec conversion and sample-rate handling.
6. Configurable VAD and caller turn detection.
7. Controlled interruptions/barge-in, including cancellation of obsolete responses.
8. Disconnect handling, timeouts, silence recovery and backpressure.
9. Structured call events and end-to-end tracing using our existing observability infrastructure.

Build a **deterministic simulated audio/telephony harness** so these behaviors can be tested without provider credentials or a live telephone call.

Do not treat UTF-8 text bytes as real playable audio in the live pipeline.

## Milestone 2 — Slice 2B: Actual voice-provider integration

Integrate the selected services:

1. Sarvam real-time STT.
2. DeepSeek Flash for conversational response generation.
3. Sarvam streaming TTS.
4. Our existing intent detection, business-context retrieval and policy engine.
5. Pipecat for coordinating real-time audio and conversations.

Preserve the rule:

**Owner policies determine what the assistant may say. The LLM determines how an authorized response is phrased.**

The model must never bypass owner rules or receive restricted business information. No unchecked LLM output should be streamed directly to speech synthesis.

Reuse the existing Slice 1 implementation wherever possible rather than rewriting it.

## Testing and observability

Build automated tests for:

- A complete price enquiry handled without disclosure.
- Multiple conversational turns.
- A customer interrupting the AI.
- Slow speech and partial transcripts.
- A caller hanging up while audio is being generated.
- Provider failures and timeouts.
- Two simultaneous independent calls.
- Conversation transcripts and outcomes being persisted accurately.
- No customer-sensitive data leaking into operational logs.
- Policy enforcement and prompt-injection attempts.

Instrument latency at every stage, especially:

- Final STT transcript.
- Intent and policy decisions.
- LLM first usable response.
- TTS first generated audio.
- First audio actually sent for playback.
- Barge-in cancellation and audio clearing.
- Call completion or failure.

Preserve existing tests, evaluations, PostgreSQL compatibility, and CI.

## Important constraints

- Verify the current official Exotel, Pipecat, Sarvam and DeepSeek APIs before implementing integrations.
- Do not assume provider adapters from examples are automatically compatible.
- Make credentials environment-configurable and never commit secrets.
- If we cannot test a real phone call without Exotel account access or credentials, say so explicitly. Deliver working mocks, provider adapters and exact live-test instructions instead.
- Do not expose unauthenticated call transcripts or customer information on a public endpoint.
- Do not add the owner mobile app, inventory integrations, WhatsApp, live owner transfer, voice cloning or unrelated MVP features.
- Keep the architecture modular and avoid unnecessary microservices.
- Work on a feature branch and provide reviewable commits or a PR.

## Definition of success

A customer can eventually call an Exotel-connected number, speak naturally in Hindi/Hinglish, receive an audible policy-compliant response from Pratham, continue across multiple turns, interrupt the assistant, and have the complete conversation and outcome recorded.

**Please execute Slice 2A first, verify it thoroughly, then proceed to Slice 2B if feasible.** Stop at a meaningful review boundary after these milestones.

When finished, provide:

- What was actually implemented.
- Architecture changes and key files.
- Tests run and their real results.
- What was tested using mocks versus actual provider services.
- Known limitations and unresolved external dependencies.
- Reproduction instructions.
- A clear implementation report for my engineering review.

Do not claim the real-phone acceptance test passed unless a real telephone call was actually performed successfully.