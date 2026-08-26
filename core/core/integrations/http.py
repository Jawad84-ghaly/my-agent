"""Transport HTTP avec retry — la couche qui absorbe les caprices des API Google.

Deux raisons de ne pas appeler `httpx` directement depuis les providers :

1. Les quotas Google se manifestent par des 429 et des 403 `rateLimitExceeded`
   en rafale. Sans backoff respectant `Retry-After`, on aggrave la situation.
2. Un provider testable ne doit pas dépendre du réseau. Le `Transport` est un
   protocole : les tests injectent un double, la production injecte httpx.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BASE_DELAY = 0.5
MAX_DELAY = 8.0


@dataclass
class Response:
    status_code: int
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.json_body


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Response: ...


class HttpError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HttpxTransport:
    """Transport réel. httpx est importé à l'usage pour garder les tests légers."""

    def __init__(self, timeout: float = 15.0) -> None:
        import httpx  # noqa: PLC0415 — import paresseux volontaire

        self._client = httpx.AsyncClient(timeout=timeout)

    async def request(self, method, url, *, headers=None, params=None, json=None) -> Response:
        raw = await self._client.request(method, url, headers=headers, params=params, json=json)
        try:
            body = raw.json()
        except ValueError:
            body = None
        return Response(raw.status_code, body, dict(raw.headers))

    async def aclose(self) -> None:
        await self._client.aclose()


def _is_rate_limited(response: Response) -> bool:
    """Google renvoie parfois 403 pour un dépassement de quota, pas 429."""
    if response.status_code == 429:
        return True
    if response.status_code != 403 or not isinstance(response.json_body, dict):
        return False
    errors = (response.json_body.get("error") or {}).get("errors") or []
    return any(
        e.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded") for e in errors
    )


def _retry_delay(attempt: int, response: Response | None) -> float:
    if response is not None:
        header = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if header:
            try:
                return min(float(header), MAX_DELAY)
            except ValueError:
                pass
    # Backoff exponentiel avec jitter : sans le jitter, plusieurs workers
    # repartent en même temps et reproduisent la rafale qu'on veut éviter.
    return min(BASE_DELAY * (2**attempt) + random.uniform(0, 0.2), MAX_DELAY)


async def request_with_retry(
    transport: Transport,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep=asyncio.sleep,
) -> Response:
    """Rejoue les erreurs transitoires. Les 4xx métier remontent immédiatement."""
    last: Response | None = None

    for attempt in range(max_attempts):
        response = await transport.request(
            method, url, headers=headers, params=params, json=json
        )
        if response.ok:
            return response

        last = response
        retryable = response.status_code in RETRYABLE_STATUS or _is_rate_limited(response)
        if not retryable or attempt == max_attempts - 1:
            break
        await sleep(_retry_delay(attempt, response))

    assert last is not None
    raise HttpError(last.status_code, _detail(last))


def _detail(response: Response) -> str:
    body = response.json_body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
    return str(body)[:200]
