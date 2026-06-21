from __future__ import annotations

import json

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.http import AsyncApiClient, wrap_transport_error
from mm_jira_bot.logging import get_logger
from mm_jira_bot.retry import ApiError

log = get_logger(__name__)

SYSTEM_PROMPT = """Ты помогаешь дежурным инженерам составлять постмортем инцидента.
Пиши на русском языке. Используй только факты из предоставленного треда и метаданных.
Не выдумывай причины, действия, метрики, имена, сервисы и таймлайны: если данных нет,
пиши "не указано" или формулируй осторожно. Все времена в хронологии указывай
по московскому времени в формате HH:MM. Не добавляй code fences и служебные пояснения,
верни только готовый отчет."""


def _parse_chat_content(response: httpx.Response) -> str:
    data = response.json()
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ApiError("LLM response did not include choices", retryable=False)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ApiError("LLM response choice did not include message", retryable=False)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError("LLM response message was empty", retryable=False)
    return content.strip()


def _extract_stream_delta(data: str) -> str | None:
    """Pull the incremental ``content`` out of one OpenAI SSE ``data:`` line."""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    piece = delta.get("content")
    return piece if isinstance(piece, str) and piece else None


def build_llm_auth_headers(settings: Settings) -> dict[str, str]:
    if settings.llm_api_token is None:
        raise ApiError("LLM API token is not configured", retryable=False)
    return {
        "Authorization": f"Bearer {settings.llm_api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class PostmortemLlmClient(AsyncApiClient):
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        client = http_client or httpx.AsyncClient(
            base_url=f"{settings.llm_base_url.rstrip('/')}/",
            # ``read`` is the gap between streamed chunks, not the whole-response
            # budget, so a long generation stays alive while a dead connection is
            # still caught quickly. ``connect`` keeps an unreachable endpoint from
            # hanging for the full read window.
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.llm_read_timeout,
                write=10.0,
                pool=10.0,
            ),
            headers=build_llm_auth_headers(settings),
        )
        super().__init__(settings, client, own_client=http_client is None, log=log)

    async def preflight_check(self) -> dict[str, object]:
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {
                    "role": "user",
                    "content": "Ответь ровно OK. Без пояснений.",
                },
            ],
            "max_tokens": min(self._settings.llm_max_tokens, 16),
        }
        content = await self._request(
            "POST",
            "chat/completions",
            json=payload,
            error_message="Failed to run LLM preflight",
            event="llm.preflight",
            parse=_parse_chat_content,
            llm_model=self._settings.llm_model,
        )
        assert isinstance(content, str)
        return {
            "llm_base_url": self._settings.llm_base_url,
            "llm_model": self._settings.llm_model,
            "llm_response_length": len(content),
        }

    async def generate_postmortem(self, prompt: str) -> str:
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self._settings.llm_max_tokens,
        }

        if not self._settings.llm_stream:
            report = await self._request(
                "POST",
                "chat/completions",
                json=payload,
                error_message="Failed to generate postmortem",
                event="llm.postmortem.generate",
                parse=_parse_chat_content,
                llm_model=self._settings.llm_model,
                prompt_length=len(prompt),
            )
            assert isinstance(report, str)
            return report

        return await self._generate_streaming(payload, prompt_length=len(prompt))

    async def _generate_streaming(self, payload: dict, *, prompt_length: int) -> str:
        stream_payload = {**payload, "stream": True}
        error_message = "Failed to generate postmortem"

        async def operation() -> str:
            try:
                async with self._client.stream(
                    "POST", "chat/completions", json=stream_payload
                ) as response:
                    if response.is_error:
                        await response.aread()
                        self._raise_for_status(response, error_message)
                    content_type = response.headers.get("content-type", "")
                    # Auto-fallback: a proxy that ignores ``stream`` answers with a
                    # buffered JSON body instead of an SSE stream — parse it as a
                    # normal chat completion rather than failing.
                    if "text/event-stream" not in content_type:
                        await response.aread()
                        return _parse_chat_content(response)
                    return await self._collect_stream(response)
            except httpx.HTTPError as exc:
                raise wrap_transport_error(error_message, exc) from exc

        return await self._retry(
            operation,
            event="llm.postmortem.generate",
            llm_model=self._settings.llm_model,
            prompt_length=prompt_length,
        )

    async def _collect_stream(self, response: httpx.Response) -> str:
        chunks: list[str] = []
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            piece = _extract_stream_delta(data)
            if piece:
                chunks.append(piece)
        report = "".join(chunks).strip()
        if not report:
            raise ApiError("LLM stream returned empty content", retryable=False)
        return report
