"""Rate-limited async client for OpenAI-compatible streaming and JSON responses."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import EndpointConfig, RequestConfig, RunnerOptions
from .errors import EndpointError
from .models import EndpointResponse, Message

Sleep = Callable[[float], Awaitable[None]]


class SlidingWindowRateLimiter:
    """FIFO limiter shared by primary and judge calls."""

    def __init__(
        self,
        max_requests: int,
        period_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.max_requests = max_requests
        self.period_seconds = period_seconds
        self._clock = clock
        self._sleep = sleep
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                while self._timestamps and now - self._timestamps[0] >= self.period_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                delay = max(0.0, self.period_seconds - (now - self._timestamps[0]))
            await self._sleep(delay)


def _completion_url(value: str) -> str:
    url = value.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith(("/v1", "/v2")):
        return f"{url}/chat/completions"
    return f"{url}/v2/chat/completions"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            return max(0.0, target.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _content_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def extract_completion_text(payload: Any) -> str:
    """Extract text from common OpenAI-compatible response and SSE shapes."""

    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = _content_value(delta.get("content"))
            if text:
                return text
        message = choice.get("message")
        if isinstance(message, dict):
            text = _content_value(message.get("content"))
            if text:
                return text
        text = _content_value(choice.get("text"))
        if text:
            return text
    for key in ("content", "text", "token", "response"):
        text = _content_value(payload.get(key))
        if text:
            return text
    return ""


class AsyncXevyoClient:
    """One shared client controls concurrency, quota, retry policy, and SSE decoding."""

    def __init__(
        self,
        endpoint: EndpointConfig,
        request: RequestConfig,
        options: RunnerOptions,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        if not endpoint.url:
            raise EndpointError("endpoint URL is empty")
        if not token:
            raise EndpointError(f"credential environment variable {endpoint.jwt_env} is empty")
        self.endpoint = endpoint
        self.request = request
        self.options = options
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(options.concurrency)
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            options.rate_limit_per_minute, sleep=sleep
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(options.timeout_seconds),
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "xeval/0.1",
            },
        )
        self._url = _completion_url(endpoint.url)

    async def __aenter__(self) -> AsyncXevyoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        chat_id: str,
        thread_id: str | None = None,
        stream: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> EndpointResponse:
        use_stream = self.request.stream if stream is None else stream
        body: dict[str, Any] = {
            "model": self.request.model,
            "messages": [message.as_dict() for message in messages],
            "stream": use_stream,
            "temperature": self.request.temperature if temperature is None else temperature,
            "max_tokens": self.request.max_tokens if max_tokens is None else max_tokens,
        }
        if self.request.send_conversation_ids:
            body["chat_id"] = chat_id
            if thread_id:
                body["thread_id"] = thread_id
        async with self._semaphore:
            return await self._send_with_retries(body, use_stream)

    async def _send_with_retries(self, body: dict[str, Any], stream: bool) -> EndpointResponse:
        last_error: Exception | None = None
        for attempt in range(1, self.options.retries + 2):
            await self._rate_limiter.acquire()
            started = time.perf_counter()
            try:
                if stream:
                    response = await self._stream_request(body, started, attempt)
                else:
                    response = await self._json_request(body, started, attempt)
                return response
            except _RetryableResponse as exc:
                last_error = exc
                if attempt > self.options.retries:
                    break
                delay = exc.retry_after
                if delay is None:
                    delay = min(
                        self.options.backoff_max_seconds,
                        self.options.backoff_initial_seconds * (2 ** (attempt - 1)),
                    )
                await self._sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt > self.options.retries:
                    break
                await self._sleep(
                    min(
                        self.options.backoff_max_seconds,
                        self.options.backoff_initial_seconds * (2 ** (attempt - 1)),
                    )
                )
        kind = type(last_error).__name__ if last_error else "unknown error"
        raise EndpointError(
            f"endpoint failed after {self.options.retries + 1} attempts ({kind})"
        ) from last_error

    async def _stream_request(
        self, body: dict[str, Any], started: float, attempt: int
    ) -> EndpointResponse:
        async with self._client.stream(
            "POST", self._url, json=body, headers={"Accept": "text/event-stream"}
        ) as response:
            await self._check_status(response)
            parts: list[str] = []
            event_data: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if event_data:
                        done = self._consume_sse_event("\n".join(event_data), parts)
                        event_data.clear()
                        if done:
                            break
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    event_data.append(line[5:].lstrip())
            if event_data:
                self._consume_sse_event("\n".join(event_data), parts)
            text = "".join(parts)
            if not text:
                raise EndpointError("stream completed without response content")
            return self._result(response, text, started, attempt)

    async def _json_request(
        self, body: dict[str, Any], started: float, attempt: int
    ) -> EndpointResponse:
        response = await self._client.post(
            self._url, json=body, headers={"Accept": "application/json"}
        )
        await self._check_status(response)
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type == "text/event-stream":
            text = self._buffered_sse_text(response.text)
            if not text:
                raise EndpointError("stream completed without response content")
            return self._result(response, text, started, attempt)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise EndpointError("endpoint returned invalid JSON") from exc
        text = extract_completion_text(payload)
        if not text:
            raise EndpointError("JSON response did not contain completion text")
        return self._result(response, text, started, attempt, payload)

    @classmethod
    def _buffered_sse_text(cls, body: str) -> str:
        """Decode a completed SSE body returned despite a non-streaming request."""

        parts: list[str] = []
        event_data: list[str] = []
        for line in body.splitlines():
            if line == "":
                if event_data:
                    done = cls._consume_sse_event("\n".join(event_data), parts)
                    event_data.clear()
                    if done:
                        break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                event_data.append(line[5:].lstrip())
        if event_data:
            cls._consume_sse_event("\n".join(event_data), parts)
        return "".join(parts)

    async def _check_status(self, response: httpx.Response) -> None:
        if response.status_code == 429 or 500 <= response.status_code < 600:
            # Consume the response so the connection can be reused, but never surface its body.
            await response.aread()
            raise _RetryableResponse(
                response.status_code, retry_after=_retry_after_seconds(response)
            )
        if response.status_code >= 400:
            await response.aread()
            raise EndpointError(f"endpoint returned HTTP {response.status_code}")

    @staticmethod
    def _consume_sse_event(data: str, parts: list[str]) -> bool:
        if data.strip() == "[DONE]":
            return True
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise EndpointError("endpoint returned malformed SSE JSON") from exc
        text = extract_completion_text(payload)
        if text:
            parts.append(text)
        return False

    def _result(
        self,
        response: httpx.Response,
        text: str,
        started: float,
        attempt: int,
        payload: Any = None,
    ) -> EndpointResponse:
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        return EndpointResponse(
            text=text,
            latency_ms=(time.perf_counter() - started) * 1000,
            endpoint_version=response.headers.get(self.endpoint.version_header, "unknown"),
            status_code=response.status_code,
            attempts=attempt,
            request_id=response.headers.get("x-request-id"),
            raw_usage=usage if isinstance(usage, dict) else {},
        )


class _RetryableResponse(Exception):
    def __init__(self, status_code: int, retry_after: float | None) -> None:
        super().__init__(f"retryable HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after
