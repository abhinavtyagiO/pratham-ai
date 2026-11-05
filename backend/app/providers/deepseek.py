import json

import httpx

from app.domain.conversations import ResponseDraft
from app.prompts.templates import VOICE_RESPONSE_PROMPT, VOICE_RESPONSE_VERSION
from app.providers.interfaces import ModelResult
from app.realtime.contracts import VoiceFailure


class DeepSeekFlash:
    name = "deepseek"
    prompt_version = VOICE_RESPONSE_VERSION

    def __init__(self, settings, *, client=None):
        self.model = settings.llm_model
        self.client = client or httpx.AsyncClient(
            base_url="https://api.deepseek.com",
            timeout=settings.provider_timeout_s,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key.get_secret_value()}"},
        )

    async def generate(self, context):
        body = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "max_tokens": 256,
            "messages": [
                {"role": "system", "content": VOICE_RESPONSE_PROMPT},
                {"role": "user", "content": context.model_dump_json()},
            ],
        }
        content, finish, tokens, done = "", None, {}, False
        async with self.client.stream("POST", "/chat/completions", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    raise VoiceFailure("MODEL_ERROR", "llm_protocol")
                raw = line[6:]
                if raw == "[DONE]":
                    done = True
                    break
                data = json.loads(raw)
                if data.get("error"):
                    raise VoiceFailure("MODEL_ERROR", "llm_provider_error", "ProviderError")
                if data.get("usage"):
                    tokens = data["usage"]
                for choice in data.get("choices", []):
                    value = choice.get("delta", {}).get("content")
                    if value:
                        content += value
                    if len(content) > 8192:
                        raise VoiceFailure("MODEL_ERROR", "llm_output_limit")
                    finish = choice.get("finish_reason") or finish
        if not done or finish != "stop" or not content.strip():
            raise VoiceFailure("MODEL_ERROR", "llm_incomplete_output")
        # No partial sentence is usable. Only a complete validated capability-limited object
        # is returned to the policy renderer; reasoning and arbitrary text are never spoken.
        draft = ResponseDraft.model_validate_json(content)
        return ModelResult(draft, tokens.get("prompt_tokens"), tokens.get("completion_tokens"))

    async def close(self):
        await self.client.aclose()
