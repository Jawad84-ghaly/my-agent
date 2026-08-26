import asyncio

import pytest

from core.graph.executor import ExecutionState, execute_plan, summarize
from core.planning import Task
from core.tools.registry import ToolRegistry, idempotency_key


@pytest.fixture
def registry():
    reg = ToolRegistry()
    calls: list[str] = []
    reg.calls = calls  # type: ignore[attr-defined]

    @reg.register("contacts.resolve")
    async def _resolve(query: str):
        calls.append("contacts.resolve")
        await asyncio.sleep(0.01)
        return {"email": "marc@exemple.fr", "name": "Marc Dubois"}

    @reg.register("calendar.detect_conflicts")
    async def _conflicts(start: str = ""):
        calls.append("calendar.detect_conflicts")
        await asyncio.sleep(0.01)
        return []

    @reg.register("calendar.create_event", mutating=True)
    async def _create(title: str, attendees=None, idempotency_key: str = ""):
        calls.append("calendar.create_event")
        return {"id": "evt-1", "title": title, "key": idempotency_key}

    @reg.register("mail.draft")
    async def _draft(to: str, subject: str = ""):
        calls.append("mail.draft")
        return {"id": "draft-1", "to": to}

    @reg.register("mail.send", mutating=True)
    async def _send(draft_id: str, idempotency_key: str = ""):
        calls.append("mail.send")
        return {"id": "sent-1"}

    @reg.register("web.search")
    async def _boom(query: str = ""):
        raise RuntimeError("réseau indisponible")

    return reg


def run(coro):
    return asyncio.run(coro)


def test_free_plan_runs_to_completion(registry):
    tasks = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "calendar.detect_conflicts", {"start": "2026-08-27T10:00"}),
        Task("T3", "calendar.create_event", {"title": "Point"}, depends_on=("T1", "T2")),
    ]
    state = run(execute_plan(tasks, registry))
    assert not state.suspended
    assert state.completed == ["T1", "T2", "T3"]


def test_independent_tasks_run_concurrently(registry):
    """Deux tâches de 10 ms en parallèle doivent coûter ~10 ms, pas 20."""
    import time

    tasks = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "calendar.detect_conflicts", {"start": "x"}),
    ]
    started = time.perf_counter()
    run(execute_plan(tasks, registry))
    assert (time.perf_counter() - started) < 0.018


def test_execution_suspends_before_sending(registry):
    tasks = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "mail.draft", {"to": "{{T1.email}}"}, depends_on=("T1",)),
        Task("T3", "mail.send", {"draft_id": "{{T2.id}}"}, depends_on=("T2",)),
    ]
    state = run(execute_plan(tasks, registry))

    assert state.suspended
    assert state.pending_approval.task.id == "T3"
    assert "mail.send" not in registry.calls  # rien n'est parti
    assert state.completed == ["T1", "T2"]


def test_resume_after_approval_sends(registry):
    tasks = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "mail.draft", {"to": "{{T1.email}}"}, depends_on=("T1",)),
        Task("T3", "mail.send", {"draft_id": "{{T2.id}}"}, depends_on=("T2",)),
    ]
    state = run(execute_plan(tasks, registry))
    resumed = run(
        execute_plan(tasks, registry, state=state, approved_task_ids=frozenset({"T3"}))
    )
    assert not resumed.suspended
    assert "mail.send" in registry.calls
    assert resumed.completed == ["T1", "T2", "T3"]


def test_completed_tasks_are_not_replayed_on_resume(registry):
    """La reprise ne doit pas recréer ce qui existe déjà."""
    tasks = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "mail.send", {"draft_id": "d"}, depends_on=("T1",)),
    ]
    state = run(execute_plan(tasks, registry))
    run(execute_plan(tasks, registry, state=state, approved_task_ids=frozenset({"T2"})))
    assert registry.calls.count("contacts.resolve") == 1


def test_failure_is_reported_not_swallowed(registry):
    state = run(execute_plan([Task("T1", "web.search", {"query": "x"})], registry))
    assert state.completed == []
    assert "réseau indisponible" in state.failures["T1"]
    assert "⚠️" in summarize(state)


def test_idempotency_key_is_stable_and_order_independent():
    a = idempotency_key("T3", "calendar.create_event", {"title": "Point", "start": "10h"})
    b = idempotency_key("T3", "calendar.create_event", {"start": "10h", "title": "Point"})
    c = idempotency_key("T3", "calendar.create_event", {"title": "Autre", "start": "10h"})
    assert a == b
    assert a != c
