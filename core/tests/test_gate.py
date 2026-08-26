from datetime import datetime, timedelta, timezone

from core.gate import (
    classify_reply,
    format_approval_summary,
    is_expired,
    plan_needs_gate,
    requires_approval,
)
from core.planning import Task


def test_reading_never_requires_approval():
    for tool in ("mail.read", "mail.search", "calendar.list_events", "contacts.resolve"):
        assert not requires_approval(Task("T", tool)).requires_approval


def test_draft_is_free_but_send_is_gated():
    assert not requires_approval(Task("T", "mail.draft")).requires_approval
    assert requires_approval(Task("T", "mail.send")).requires_approval


def test_event_without_attendees_is_free():
    task = Task("T", "calendar.create_event", {"title": "Bloquer 2 h"})
    assert not requires_approval(task).requires_approval


def test_event_with_attendees_is_gated():
    task = Task("T", "calendar.create_event", {"attendees": ["marc@exemple.fr"]})
    decision = requires_approval(task)
    assert decision.requires_approval
    assert "participant" in decision.reason


def test_empty_attendee_list_stays_free():
    task = Task("T", "calendar.create_event", {"attendees": []})
    assert not requires_approval(task).requires_approval


def test_unknown_tool_is_closed_by_default():
    """Ajouter un outil sortant sans y penser ne doit pas ouvrir de brèche."""
    decision = requires_approval(Task("T", "sms.blast_to_everyone"))
    assert decision.requires_approval


def test_plan_needs_gate_lists_every_flagged_task():
    tasks = [
        Task("T1", "contacts.resolve"),
        Task("T2", "mail.draft"),
        Task("T3", "mail.send"),
        Task("T4", "calendar.delete_event"),
    ]
    flagged = [t.id for t, _ in plan_needs_gate(tasks)]
    assert flagged == ["T3", "T4"]


def test_approval_expires_after_thirty_minutes():
    now = datetime.now(timezone.utc)
    assert not is_expired(now - timedelta(minutes=29), now)
    assert is_expired(now - timedelta(minutes=31), now)


def test_naive_timestamp_is_treated_as_utc():
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(minutes=5)).replace(tzinfo=None)
    assert not is_expired(naive, now)


def test_reply_classification():
    assert classify_reply("ok") == "approve"
    assert classify_reply("  OK! ") == "approve"
    assert classify_reply("oui") == "approve"
    assert classify_reply("non") == "reject"
    assert classify_reply("annule") == "reject"


def test_ambiguous_reply_is_never_an_approval():
    """Un doute doit coûter un aller-retour, pas un email envoyé par erreur."""
    for text in ("ok mais change l'objet", "plutôt mardi", "ça dépend", "okay?"):
        assert classify_reply(text) == "edit"


def test_summary_shows_recipient_and_subject():
    task = Task("T", "mail.send")
    summary = format_approval_summary(
        task,
        {
            "to_display": "Marc Dubois <marc@exemple.fr>",
            "subject": "Confirmation RDV mardi 10 h",
            "body": "Bonjour Marc,\nJe te confirme notre RDV.\nÀ mardi.",
        },
    )
    assert "Marc Dubois" in summary
    assert "Confirmation RDV mardi 10 h" in summary
    assert "*ok*" in summary
