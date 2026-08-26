"""Double de transport HTTP : les tests n'ouvrent jamais de connexion réseau."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from core.integrations.http import Response


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any]
    json: Any


@dataclass
class FakeTransport:
    """Rejoue des réponses scriptées et enregistre ce qui a été envoyé.

    `handler` reçoit la requête et l'index d'appel, et renvoie une Response —
    ce qui permet de simuler un 429 suivi d'un succès, ou un 410 sur syncToken.
    """

    handler: Callable[[RecordedRequest, int], Response]
    requests: list[RecordedRequest] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)

    async def request(self, method, url, *, headers=None, params=None, json=None) -> Response:
        recorded = RecordedRequest(method, url, headers or {}, params or {}, json)
        self.requests.append(recorded)
        return self.handler(recorded, len(self.requests) - 1)

    async def sleep(self, seconds: float) -> None:
        """Remplace asyncio.sleep : les tests de backoff restent instantanés."""
        self.slept.append(seconds)


def sequence(*responses: Response) -> Callable[[RecordedRequest, int], Response]:
    """Renvoie les réponses dans l'ordre, puis répète la dernière."""

    def handler(_request: RecordedRequest, index: int) -> Response:
        return responses[min(index, len(responses) - 1)]

    return handler


def always(response: Response) -> Callable[[RecordedRequest, int], Response]:
    return lambda _request, _index: response


@pytest.fixture
def transport_factory():
    def make(handler) -> FakeTransport:
        return FakeTransport(handler)

    return make
