import asyncio

from core.gate import GATED_TOOLS, format_approval_summary, requires_approval
from core.planning import Task
from core.providers.mail import InMemoryMailbox
from core.tools.mail_tools import register_mail_tools
from core.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def registry_with(mailbox: InMemoryMailbox | None = None) -> tuple[ToolRegistry, InMemoryMailbox]:
    mailbox = mailbox or InMemoryMailbox()
    registry = ToolRegistry()
    register_mail_tools(registry, mailbox)
    return registry, mailbox


def test_draft_then_send_delivers_the_message():
    registry, mailbox = registry_with()
    draft = run(registry.call("mail.draft", {"to": ["marc@exemple.fr"], "subject": "Point",
                                             "body": "Salut"}, task_id="t1")).unwrap()
    assert draft["to_display"] == "marc@exemple.fr"

    sent = run(registry.call(
        "mail.send",
        {"draft_id": draft["id"], "to": draft["to"], "to_display": draft["to_display"],
         "subject": draft["subject"], "body": draft["body"]},
        task_id="t2",
    )).unwrap()
    assert len(mailbox.sent) == 1
    assert mailbox.sent[0].id == sent["id"]


def test_send_is_idempotent_on_retry():
    """Un retry après timeout réseau ne doit pas envoyer un second email identique."""
    registry, mailbox = registry_with()
    draft = run(registry.call("mail.draft", {"to": ["a@x.fr"], "subject": "S", "body": "B"},
                               task_id="t1")).unwrap()
    args = {"draft_id": draft["id"]}
    first = run(registry.call("mail.send", args, task_id="t2")).unwrap()
    second = run(registry.call("mail.send", args, task_id="t2"))  # même task_id, même clé dérivée
    assert second.unwrap()["id"] == first["id"]
    assert len(mailbox.sent) == 1


def test_mail_send_is_gated_but_draft_is_free():
    assert "mail.send" in GATED_TOOLS
    assert requires_approval(Task("t2", "mail.send", {"draft_id": "d1"})).requires_approval
    assert not requires_approval(Task("t1", "mail.draft", {"to": ["a@x.fr"]})).requires_approval


def test_approval_summary_shows_recipient_and_excerpt():
    task = Task("t2", "mail.send", {"draft_id": "d1"})
    prepared = {"to_display": "marc@exemple.fr", "subject": "Point produit",
                "body": "Bonjour,\nÀ demain."}
    summary = format_approval_summary(task, prepared)
    assert "marc@exemple.fr" in summary
    assert "Point produit" in summary
