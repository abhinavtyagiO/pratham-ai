# Live voice pilot: setup and acceptance procedure

This is an operator procedure. **No real phone call or paid provider request has been performed
by the implementation run.** The protocol and lifecycle tests use local mocks. Use a direct
Exotel number first; conditional forwarding from the owner's existing mobile is a later test.

## 1. Install and verify locally

Use Python 3.12, from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e backend
cd backend
make migrate
make seed
make test
make eval
make simulate-audio
```

`reports/audio-simulation/playback.wav` is an audible PCM tone test. It proves buffering, packet
conversion, and playback transport; it is not a sample of Sarvam's synthesized voice. The
accompanying `call.json` labels the simulation, ordered messages, decisions, and delivery states.

For PostgreSQL, start the existing Docker runtime, then:

```sh
docker compose -f ../compose.yaml up -d --wait
export DATABASE_URL=postgresql+psycopg://pa:pa_local@localhost:5432/pa
make migrate
make seed
TEST_POSTGRES_URL="$DATABASE_URL" make test
```

The PostgreSQL test uses a unique temporary schema and tests both Slice 1 and a real-PCM voice
session. Its role needs CREATE SCHEMA privileges. Existing business tables are not test fixtures.

## 2. Configure credentials and the pilot business

Copy `backend/.env.example` to `backend/.env` if `.env` does not exist. Edit that untracked file;
do not paste secrets into chat, source, commands with literal keys, or committed reports.
Generate independent long random values for `API_KEY` and `VOICE_EXOTEL_WS_PASSWORD` using a
password manager or `python -c 'import secrets; print(secrets.token_urlsafe(32))'` locally.

Set:

```dotenv
VOICE_ENABLED=true
VOICE_MODE=live
VOICE_VAD_MODE=sarvam
VOICE_EXOTEL_WS_USER=pratham
VOICE_EXOTEL_WS_PASSWORD=<independent-random-ingress-secret>
VOICE_EXOTEL_ACCOUNT_SID=<your-Exotel-account-SID>
VOICE_EXOTEL_TO_NUMBER=<exact-destination-number-in-Exotel-start-event>
VOICE_BUSINESS_ID=00000000-0000-0000-0000-000000000001
VOICE_SARVAM_API_KEY=<Sarvam-subscription-key>
VOICE_DEEPSEEK_API_KEY=<DeepSeek-key>
API_KEY=<independent-random-administrator-secret>
VOICE_STT_MODEL=saaras:v4
VOICE_TTS_MODEL=bulbul:v4-flash
VOICE_LLM_MODEL=deepseek-flash
VOICE_LANGUAGE_CODE=hi-IN
VOICE_TTS_SPEAKER=shubh_hi_customer
VOICE_MIN_SPEECH_MS=250
VOICE_SILENCE_MS=700
VOICE_VAD_THRESHOLD=0.3
```

Keep `VOICE_LANGUAGE_CODE=hi-IN` for this initial Hindi/Hinglish pilot. Native Hindi speech
templates are reviewed equivalents of the authorized Slice 1 phrases. Script, voice, and
language switching beyond this pilot are not implemented.

The WebSocket Basic credentials are a secret **for your Pratham server**, configured into the
Exotel Voicebot applet. They do not need to be your Exotel control-plane API credentials.
Inbound calls only require Exotel number/flow access; this backend does not originate calls
and does not need an Exotel API token to receive media.

Check configuration without making a network request:

```sh
../.venv/bin/python -m app.voice_preflight
```

This lists missing variable names and model IDs; it does not print keys or verify account
entitlements. Live startup fails if required provider/ingress settings are missing. A provider
subscription, balance, model availability, and Saaras realtime enablement must be verified in
the provider accounts. There is no silent fallback from live mode to tone mocks.

Replace the seeded demo store name/address with owner-confirmed information before a pilot.
The price rule is already seeded, enabled, and closes at 18:30 Asia/Kolkata. Arbitrary natural
language rule interpretation and inventory remain outside this milestone.

## 3. Serve a secure WebSocket endpoint

Run one application worker:

```sh
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Place it behind your existing TLS reverse proxy, exposing `/voice/exotel` as WSS. The proxy must
forward WebSocket upgrades and the `Authorization` header, permit the configured call duration,
and enforce a reasonable WebSocket frame limit (the application rejects text frames over 150 KB).
Disable request-header/body recording. Keep administrator routes internal where possible; they
require Bearer authentication even if reachable. A public `/health` returns no customer data.

Use a trusted TLS certificate. No public deployment, tunnel, DNS, proxy, or firewall change is
performed by this code. The in-process connection registry is intentionally sized for a
single-worker pilot; multi-worker admission/reconnect routing is deferred.

## 4. Configure the direct Exotel number

In the Exotel dashboard:

1. Create an App Bazaar flow with a **Voicebot** applet for bidirectional streaming. The Stream
   applet is listen-only and cannot play replies. Follow any greeting requirement shown for the
   account/routing product; Pratham also speaks its own explicit AI-assistant greeting.
2. Configure its WebSocket URL as:
   `wss://pratham:<URL-encoded-ingress-password>@<your-TLS-host>/voice/exotel?sample-rate=8000`.
   Exotel turns userinfo into an HTTP Basic Authorization header. Keep this URL private in the
   dashboard. The application never accepts an ingress secret in a query parameter.
3. Configure 8 kHz PCM streaming for the first test, then map the direct ExoPhone to this flow.
   16 and 24 kHz Exotel PCM are also accepted and resampled internally. Mulaw/Twilio envelopes
   are rejected rather than misinterpreted.
4. Confirm the destination number format matches `VOICE_EXOTEL_TO_NUMBER`. The account SID in
   the start event must match `VOICE_EXOTEL_ACCOUNT_SID`; these checks follow Basic authentication.

Some account UI details and Voicebot/realtime availability require Exotel support. The exact
production handshake, stream-close reason, and mark/clear timing must be observed on that account.
The implementation follows the current official protocol and deliberately does not claim
carrier compatibility from a mocked test alone.

## 5. Perform and record the actual phone acceptance test

From a normal phone, call the direct Exotel number and:

1. Listen for the explicit AI-assistant greeting; verify audible speech, volume, and no noise
   from WAV headers or sample-rate mismatch.
2. Ask “MRF 195/65 R15 ka rate kya hai?” Confirm a store invitation, the correct 6:30 closing
   time when mentioned, and no price or invented stock claim.
3. Ask another price question in the same call. Pause in the middle of a sentence to test slow
   speech and turn detection. Confirm no answer is triggered by a partial transcript.
4. Interrupt while the assistant is speaking. Verify old audio stops and the subsequent answer
   addresses the new authorized price request without an old response resuming.
5. Let the call become silent. Verify one recovery prompt, then termination after the configured
   timeout. Also test hanging up while a response is being generated.
6. Run two calls concurrently. Verify the transcripts and responses remain separate.

Use synthetic customer details during initial testing. Record the test date, account/number
identifiers safely, call ID, trace ID, human listening observations, and measured timings in a
private acceptance record. Do not label a WAV/mock/adapter test as a real-phone acceptance pass.

Operational JSON events expose call/trace IDs and stage timing, without transcript or phone text.
Retrieve the corresponding transcript as the authorized operator:

```sh
curl -H "Authorization: Bearer ${API_KEY}" \
  "https://<your-TLS-host>/calls/<call-id>"
```

Make `API_KEY` available to your local shell without putting a literal key in command history.
Inspect `messages[].delivery_status`, `voice_turns`, `policy_decisions`, `model_invocations`,
`failures`, and `outcome`. `PLAYED` means the Exotel mark was acknowledged; human comprehension
still requires the listening test. An interrupted/unconfirmed turn conservatively yields
`NEEDS_ATTENTION`, even if a subsequent turn was answered.

## Failure and latency interpretation

- `stt_final`: receipt of a final transcript; elapsed time from speech-end when available.
  If the provider sends a final without a preceding speech-end event, latency is `null`.
- `intent_detected` / `policy_evaluated`: deterministic decisions; no caller prompt reaches the
  response model. Business/rules are reloaded every turn.
- `llm_first_usable_response`: a complete, schema-validated DeepSeek response. Partial SSE text
  and reasoning are withheld from TTS. DeepSeek is explicitly put in non-thinking mode.
- `tts_first_audio`: first validated/decoded PCM returned by Bulbul, including connection cost.
- `first_audio_sent`: first PCM packet successfully written to the Exotel WebSocket. It is not
  the same as physical playback at the receiver.
- `audio_cleared` / `barge_in_cancelled`: queue invalidation, response task cancellation, and
  serialized Exotel clear. Late marks cannot mark the cancelled message as played.
- `playback_acknowledged`: Exotel's mark receipt. Missing marks lead to attention/failure.
- Model failure uses one deterministic authorized response and marks owner attention. STT/TTS
  failure ends the session cleanly with partial records; there is no automatic socket reconnect
  that could replay customer audio or create duplicate turns.

Current defaults: provider stall 8 s, socket send 2 s, playback ACK 12 s, missing media 20 s,
silence prompt 10 s, silence close 30 s, maximum utterance 30 s, and call maximum 60 minutes.
These are bounded engineering defaults, not measured production SLOs. Tune VAD against real
Hindi/Hinglish calls, noise, echo, and accents before accepting the pilot.

## Official references checked on 5 November 2026

- [Exotel protocol](https://developer.exotel.com/docs/agentstream/websocket-protocol)
- [Voicebot applet configuration/authentication](https://developer.exotel.com/docs/agentstream/stream-voicebot-applet)
- [Pipecat Exotel](https://docs.pipecat.ai/pipecat/telephony/exotel-websockets)
- [Sarvam realtime STT](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming)
- [Sarvam TTS WebSocket contract](https://docs.sarvam.ai/api-reference/text-to-speech/stream)
- [Bulbul v4 Flash guidance](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/best-practices)
- [DeepSeek thinking toggle](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/)

The Sarvam guides disagree on some optional codecs and default voices. This implementation
sets all model/voice/rate/codec parameters explicitly and uses WAV, which their v4 guide and
endpoint contract both document. Its RIFF parser checks PCM16, mono, and the requested rate.
Live account behavior is still an external validation step.
