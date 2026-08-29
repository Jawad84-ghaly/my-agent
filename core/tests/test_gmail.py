import asyncio
import base64

import pytest

from core.integrations.http import HttpError, Response
from core.providers.gmail import GmailProvider, _encode_mime
from core.providers.mail import InMemoryIdempotencyStore

from conftest import FakeTransport, always


def run(coro):
    return asyncio.run(coro)


async def token():
    return "at-1"


def gmail_with(handler) -> tuple[GmailProvider, FakeTransport]:
    transport = FakeTransport(handler)
    return GmailProvider(transport, token), transport


def test_encode_mime_carries_recipients_and_subject():
    raw = _encode_mime(["a@x.fr"], "Objet", "Corps", cc=["b@x.fr"])
    decoded = base64.urlsafe_b64decode(raw.encode()).decode()
    assert "To: a@x.fr" in decoded
    assert "Cc: b@x.fr" in decoded
    assert "Subject: Objet" in decoded
    assert "Corps" in decoded


def test_create_draft_posts_encoded_message():
    gmail, transport = gmail_with(always(Response(200, {"id": "draft-1"})))
    draft = run(gmail.create_draft(["a@x.fr"], "Objet", "Corps", "key-1"))
    assert draft.id == "draft-1"
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].url.endswith("/drafts")
    assert "raw" in transport.requests[0].json["message"]


def test_create_draft_is_idempotent():
    """Google ne dédoublonne pas un brouillon : c'est notre store qui doit le faire."""
    gmail, transport = gmail_with(always(Response(200, {"id": "draft-1"})))
    first = run(gmail.create_draft(["a@x.fr"], "Objet", "Corps", "key-1"))
    second = run(gmail.create_draft(["a@x.fr"], "Objet", "Corps", "key-1"))
    assert first.id == second.id == "draft-1"
    assert len(transport.requests) == 1  # le second appel ne touche pas le réseau


def test_send_draft_posts_the_draft_id():
    gmail, transport = gmail_with(always(Response(200, {"id": "msg-1", "threadId": "th-1"})))
    message = run(gmail.send_draft("draft-1", "key-send"))
    assert message.id == "msg-1"
    assert message.thread_id == "th-1"
    assert transport.requests[0].url.endswith("/drafts/send")
    assert transport.requests[0].json == {"id": "draft-1"}


def test_send_draft_is_idempotent():
    """Sans ce garde-fou, un retry après timeout enverrait un second email identique."""
    gmail, transport = gmail_with(always(Response(200, {"id": "msg-1"})))
    first = run(gmail.send_draft("draft-1", "key-send"))
    second = run(gmail.send_draft("draft-1", "key-send"))
    assert first.id == second.id == "msg-1"
    assert len(transport.requests) == 1


def test_other_errors_still_propagate():
    gmail, _ = gmail_with(always(Response(403, {"error": {"message": "forbidden"}})))
    with pytest.raises(HttpError):
        run(gmail.create_draft(["a@x.fr"], "Objet", "Corps", "key-err"))


def test_shared_dedup_store_covers_two_independent_provider_instances():
    """C'est ce qui rend le dédoublonnage utile en production : un retry sur un
    nouveau job ARQ construit un nouveau `GmailProvider`, mais partage le store."""
    dedup = InMemoryIdempotencyStore()
    transport = FakeTransport(always(Response(200, {"id": "draft-1"})))
    first = GmailProvider(transport, token, dedup)
    run(first.create_draft(["a@x.fr"], "Objet", "Corps", "key-1"))

    second = GmailProvider(transport, token, dedup)
    run(second.create_draft(["a@x.fr"], "Objet", "Corps", "key-1"))
    assert len(transport.requests) == 1
