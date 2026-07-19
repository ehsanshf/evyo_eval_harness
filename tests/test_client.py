from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest

from xeval.client import AsyncXevyoClient
from xeval.config import EndpointConfig, RequestConfig, RunnerOptions
from xeval.errors import EndpointError
from xeval.models import Message

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _client(
    handler: Handler,
    *,
    retries: int = 0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    expected_version: str | None = None,
) -> AsyncXevyoClient:
    async def no_sleep(_: float) -> None:
        return None

    return AsyncXevyoClient(
        EndpointConfig(
            url="https://staging.example.test",
            jwt_env="TEST_JWT",
            version_header="X-Service-Version",
            expected_version=expected_version,
        ),
        RequestConfig(model="fixture-model", stream=True, temperature=0.2, max_tokens=50),
        RunnerOptions(retries=retries, concurrency=2, rate_limit_per_minute=60),
        "credential-secret",
        transport=httpx.MockTransport(handler),
        sleep=sleep or no_sleep,
    )


@pytest.mark.asyncio
async def test_streaming_completion_parses_sse_and_sends_expected_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = (
            b": keep-alive\n\n"
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":[{"text":" world"}]}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "X-Service-Version": "endpoint-v7",
                "X-Request-ID": "request-123",
            },
            content=content,
        )

    async with _client(handler) as client:
        result = await client.complete(
            (Message("user", "hello"),), chat_id="chat-1", thread_id="thread-2"
        )

    assert result.text == "Hello world"
    assert result.endpoint_version == "endpoint-v7"
    assert result.request_id == "request-123"
    assert result.attempts == 1
    assert str(requests[0].url) == "https://staging.example.test/v2/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer credential-secret"
    body = json.loads(requests[0].content)
    assert body == {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "chat_id": "chat-1",
        "temperature": 0.2,
        "max_tokens": 50,
        "thread_id": "thread-2",
    }


@pytest.mark.asyncio
async def test_non_streaming_completion_parses_message_parts_and_usage() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Service-Version": "endpoint-v8"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "part one"},
                                {"type": "text", "text": " and two"},
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        result = await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)

    assert result.text == "part one and two"
    assert result.endpoint_version == "endpoint-v8"
    assert result.raw_usage == {"prompt_tokens": 4, "completion_tokens": 3}


@pytest.mark.asyncio
async def test_429_honours_retry_after_and_returns_attempt_count() -> None:
    call_count = 0
    delays: list[float] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "1.25"},
                content=b"raw upstream secret that must stay private",
            )
        return httpx.Response(
            200,
            headers={"X-Service-Version": "endpoint-v9"},
            json={"choices": [{"message": {"content": "recovered"}}]},
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with _client(handler, retries=1, sleep=record_sleep) as client:
        result = await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)

    assert call_count == 2
    assert delays == [1.25]
    assert result.text == "recovered"
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_or_expose_raw_body_or_token() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            content=b"credential-secret: internal authentication diagnostics",
        )

    async with _client(handler, retries=3) as client:
        with pytest.raises(EndpointError) as caught:
            await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)

    message = str(caught.value)
    assert calls == 1
    assert message == "endpoint returned HTTP 401"
    assert "credential-secret" not in message
    assert "diagnostics" not in message


@pytest.mark.asyncio
async def test_exhausted_retry_error_is_sanitised() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"private upstream response body")

    async with _client(handler, retries=1) as client:
        with pytest.raises(EndpointError) as caught:
            await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)

    message = str(caught.value)
    assert "after 2 attempts" in message
    assert "private upstream" not in message


@pytest.mark.asyncio
async def test_missing_version_header_is_never_assumed_from_configuration() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"text": "ok"}]})

    async with _client(handler, expected_version="configured-v1") as client:
        result = await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)

    assert result.endpoint_version == "unknown"


@pytest.mark.asyncio
async def test_invalid_sse_and_json_envelopes_raise_safe_errors() -> None:
    def bad_sse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: not-json\n\n")

    async with _client(bad_sse) as client:
        with pytest.raises(EndpointError, match="malformed SSE JSON"):
            await client.complete((Message("user", "hello"),), chat_id="chat")

    def bad_json(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async with _client(bad_json) as client:
        with pytest.raises(EndpointError, match="invalid JSON"):
            await client.complete((Message("user", "hello"),), chat_id="chat", stream=False)
