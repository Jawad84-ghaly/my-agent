import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.approvals import ApprovalRegistry
from core.messaging import ChannelFormatter, RecordingSender, split_message, to_whatsapp
from core.pipeline import IncomingMessage, Pipeline
from core.planning import Task
from core.tools.registry import ToolRegistry
from core.workers import Priority, resource_lock

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# --- doubles ---------------------------------------------------------------

class FakeRouter:
    def __init__(self, trivial=False):
        self.trivial = trivial

    async def route(self, text, context):
        return {"complexity": "trivial" if self.trivial else "standard",
                "requires_tools": not self.trivial}


class FakePlanner:
    def __init__(self, plan=None):
        self.plan_value = plan or []
        self.calls = 0

    async def plan(self, text, context):
        self.calls += 1
        return list(self.plan_value)


class FakeResponder:
    async def summarize(self, text, state, context):
        if state.failures:
            return "⚠️ " + " ; ".join(state.failures.values())
        return f"✅ {len(state.completed)} action(s) effectuée(s)."


class FakeTranscriber:
    def __init__(self, transcript, confidence):
        self.transcript, self.confidence = transcript, confidence

    async def transcribe(self, media_url):
        return self.transcript, self.confidence


def build(plan=None, trivial=False, transcriber=None):
    tools = ToolRegistry()
    sent = []

    @tools.register("contacts.resolve")
    async def _resolve(query: str = ""):
        sent.append("resolve")
        return {"email": "marc@exemple.fr"}

    @tools.register("mail.draft")
    async def _draft(to: str = "", subject: str = ""):
        sent.append("draft")
        return {"id": "d1", "to": to}

    @tools.register("mail.send", mutating=True)
    async def _send(draft_id: str = "", idempotency_key: str = ""):
        sent.append("send")
        return {"id": "sent-1"}

    sender = RecordingSender()
    pipe = Pipeline(
        tools=tools,
        router=FakeRouter(trivial),
        planner=FakePlanner(plan),
        responder=FakeResponder(),
        sender=sender,
        approvals=ApprovalRegistry(),
        transcriber=transcriber,
    )
    return pipe, sender, sent


def msg(text="fais un truc", kind="text", media_url=None):
    return IncomingMessage("m1", "u1", "t1", "whatsapp", "33612345678",
                           kind=kind, text=text, media_url=media_url)


# --- chemin nominal --------------------------------------------------------

def test_trivial_request_skips_planning():
    pipe, sender, _ = build(trivial=True)
    pipe.planner = FakePlanner()
    run(pipe.handle(msg("bonjour"), T0))
    assert pipe.planner.calls == 0
    assert len(sender.sent) == 1


def test_free_plan_runs_and_reports():
    plan = [Task("T1", "contacts.resolve", {"query": "Marc"})]
    pipe, sender, calls = build(plan)
    run(pipe.handle(msg(), T0))
    assert calls == ["resolve"]
    assert "1 action" in sender.sent[0][1]


# --- gate et reprise -------------------------------------------------------

def gated_plan():
    return [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "mail.draft", {"to": "{{T1.email}}"}, depends_on=("T1",)),
        Task("T3", "mail.send", {"draft_id": "{{T2.id}}"}, depends_on=("T2",)),
    ]


def test_outbound_action_suspends_and_asks():
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    assert "send" not in calls           # rien n'est parti
    assert "Prêt à envoyer" in sender.sent[0][1]
    assert pipe.approvals.get("t1", T0) is not None


def test_ok_resumes_and_sends():
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    run(pipe.handle(msg("ok"), T0 + timedelta(minutes=1)))
    assert "send" in calls
    assert pipe.approvals.get("t1", T0) is None


def test_resume_does_not_replay_completed_tasks():
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    run(pipe.handle(msg("ok"), T0 + timedelta(minutes=1)))
    assert calls.count("resolve") == 1
    assert calls.count("draft") == 1


def test_non_resumes_cancels_without_sending():
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    run(pipe.handle(msg("non"), T0 + timedelta(minutes=1)))
    assert "send" not in calls
    assert "annulé" in sender.sent[-1][1]


def test_ambiguous_reply_replans_instead_of_sending():
    """« ok mais change l'objet » ne doit jamais partir tel quel."""
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    run(pipe.handle(msg("ok mais change l'objet"), T0 + timedelta(minutes=1)))
    assert "send" not in calls
    assert pipe.planner.calls == 2       # replanification


def test_expired_approval_is_not_honoured():
    """Un « ok » tapé une heure plus tard répond à autre chose."""
    pipe, sender, calls = build(gated_plan())
    run(pipe.handle(msg(), T0))
    run(pipe.handle(msg("ok"), T0 + timedelta(minutes=31)))
    assert "send" not in calls


def test_only_one_pending_approval_per_thread():
    pipe, _, _ = build(gated_plan())
    run(pipe.handle(msg(), T0))
    first = pipe.approvals.get("t1", T0)
    run(pipe.handle(msg("autre demande"), T0 + timedelta(minutes=1)))
    assert pipe.approvals.get("t1", T0) is not first
    assert len(pipe.approvals.pending) == 1


# --- vocal -----------------------------------------------------------------

def test_confident_transcript_is_processed():
    plan = [Task("T1", "contacts.resolve", {"query": "Marc"})]
    pipe, sender, calls = build(plan, transcriber=FakeTranscriber("appelle Marc", 0.95))
    run(pipe.handle(msg(kind="audio", text=None, media_url="https://x/a.ogg"), T0))
    assert calls == ["resolve"]


def test_low_confidence_asks_to_repeat_instead_of_guessing():
    pipe, sender, calls = build([Task("T1", "contacts.resolve")],
                                transcriber=FakeTranscriber("marmonnement", 0.3))
    run(pipe.handle(msg(kind="audio", text=None, media_url="https://x/a.ogg"), T0))
    assert calls == []
    assert "répéter" in sender.sent[0][1]


def test_empty_message_is_not_planned():
    pipe, sender, _ = build([Task("T1", "contacts.resolve")])
    run(pipe.handle(msg(""), T0))
    assert pipe.planner.calls == 0


# --- formatage WhatsApp ----------------------------------------------------

def test_markdown_is_converted_to_whatsapp_syntax():
    out = to_whatsapp("**gras** et *italique*")
    assert "*gras*" in out
    assert "_italique_" in out


def test_headings_become_bold():
    assert to_whatsapp("## Titre") == "*Titre*"


def test_tables_are_flattened():
    out = to_whatsapp("| Nom | Heure |\n|---|---|\n| Marc | 10h |")
    assert "|" not in out
    assert "Marc — 10h" in out


def test_code_blocks_survive_conversion():
    out = to_whatsapp("Voici `**du code**` intact")
    assert "`**du code**`" in out


def test_long_message_is_split_on_natural_boundaries():
    text = "\n\n".join(f"Paragraphe {i} " + "x" * 200 for i in range(30))
    chunks = split_message(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1100 for c in chunks)
    assert "(1/" in chunks[0]


def test_short_message_is_not_split_or_numbered():
    assert split_message("court") == ["court"]


def test_desktop_keeps_rich_markdown():
    rendered = ChannelFormatter("desktop").render("| a | b |\n|---|---|")
    assert "|" in rendered[0]


# --- worker ----------------------------------------------------------------

def test_queues_are_distinct_per_priority():
    names = {p.queue for p in Priority}
    assert len(names) == 4
    assert Priority.INTERACTIVE.queue != Priority.BACKGROUND.queue


def test_resource_lock_is_scoped_per_user_and_resource():
    assert resource_lock("u1", "primary") != resource_lock("u2", "primary")
    assert resource_lock("u1", "primary") != resource_lock("u1", "work")
