"""Tests du canal `app` (`core.api.app_channel`) : appairage puis échange
synchrone, sans réseau ni Redis réel — un faux pool ARQ tient lieu de worker.
"""

import asyncio

from httpx import ASGITransport, AsyncClient

import core.api.main as main
from core.db.repositories import PostgresChannelRegistry
from core.db.session import create_all, make_engine, make_session_factory


def run(coro):
    return asyncio.run(coro)


class FakeJob:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._result


class FakeArqPool:
    def __init__(self, reply: str = "réponse de Nova") -> None:
        self.reply = reply
        self.calls: list[tuple] = []
        self._exc: Exception | None = None

    def fail_next_with(self, exc: Exception) -> None:
        self._exc = exc

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return FakeJob(self.reply, self._exc)


async def _with_app(body, *, arq_pool=None):
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    main.app.state.session_factory = make_session_factory(engine)
    main.app.state.arq_pool = arq_pool or FakeArqPool()
    try:
        transport = ASGITransport(app=main.app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            await body(client, main.app.state.arq_pool)
    finally:
        await engine.dispose()


def test_pair_rejects_invalid_code():
    async def body(client, _pool):
        r = await client.post("/app/pair", params={"code": "000000"})
        assert r.status_code == 400

    run(_with_app(body))


def test_pair_issues_a_token():
    async def body(client, _pool):
        async with main.app.state.session_factory() as session:
            code = await PostgresChannelRegistry(session).issue_code("u1")
            await session.commit()

        r = await client.post("/app/pair", params={"code": code})
        assert r.status_code == 200
        token = r.json()["token"]
        assert len(token) > 20

    run(_with_app(body))


def test_pair_code_is_single_use():
    async def body(client, _pool):
        async with main.app.state.session_factory() as session:
            code = await PostgresChannelRegistry(session).issue_code("u1")
            await session.commit()

        first = await client.post("/app/pair", params={"code": code})
        assert first.status_code == 200
        second = await client.post("/app/pair", params={"code": code})
        assert second.status_code == 400

    run(_with_app(body))


def test_messages_rejects_missing_authorization():
    async def body(client, _pool):
        r = await client.post("/app/messages", params={"text": "salut"})
        assert r.status_code == 401

    run(_with_app(body))


def test_messages_rejects_unknown_token():
    async def body(client, _pool):
        r = await client.post(
            "/app/messages", params={"text": "salut"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert r.status_code == 401

    run(_with_app(body))


def test_messages_returns_the_job_result_for_a_paired_token():
    pool = FakeArqPool(reply="Bonjour ! Je peux t'aider ?")

    async def body(client, _pool):
        async with main.app.state.session_factory() as session:
            code = await PostgresChannelRegistry(session).issue_code("u1")
            await session.commit()
        pair = await client.post("/app/pair", params={"code": code})
        token = pair.json()["token"]

        r = await client.post(
            "/app/messages", params={"text": "salut"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json() == {"reply": "Bonjour ! Je peux t'aider ?"}

        job_name, args, _kwargs = pool.calls[0]
        assert job_name == "handle_message_job"
        assert args[0] == "u1"
        assert args[1]["channel"] == "app"
        assert args[1]["text"] == "salut"

    run(_with_app(body, arq_pool=pool))


def test_cors_preflight_allows_the_web_app_to_call_messages():
    """Sans CORSMiddleware, le navigateur bloquerait la réponse avant même que
    le code Dart de l'app web ne la voie — voir core/api/main.py."""

    async def body(client, _pool):
        r = await client.options(
            "/app/messages",
            headers={
                "Origin": "https://nova-web.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
        assert "POST" in r.headers["access-control-allow-methods"]

    run(_with_app(body))


def test_messages_surfaces_a_timeout_as_504():
    pool = FakeArqPool()
    pool.fail_next_with(TimeoutError())

    async def body(client, _pool):
        async with main.app.state.session_factory() as session:
            code = await PostgresChannelRegistry(session).issue_code("u1")
            await session.commit()
        pair = await client.post("/app/pair", params={"code": code})
        token = pair.json()["token"]

        r = await client.post(
            "/app/messages", params={"text": "salut"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 504

    run(_with_app(body, arq_pool=pool))
