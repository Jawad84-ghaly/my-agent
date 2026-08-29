import asyncio

from core.providers.google_people import GooglePeopleProvider
from core.integrations.http import Response

from conftest import FakeTransport, always


def run(coro):
    return asyncio.run(coro)


async def token():
    return "at-1"


def person(**overrides) -> dict:
    base = {
        "resourceName": "people/c1",
        "names": [{"displayName": "Marc Dubois"}],
        "emailAddresses": [{"value": "marc@exemple.fr"}],
        "phoneNumbers": [{"value": "+33612345678"}],
        "organizations": [{"name": "Vitagro"}],
    }
    return base | overrides


def test_connections_are_converted_to_contacts():
    transport = FakeTransport(always(Response(200, {"connections": [person()]})))
    provider = GooglePeopleProvider(transport, token)
    contacts = run(provider.list_contacts())
    assert len(contacts) == 1
    assert contacts[0].display_name == "Marc Dubois"
    assert contacts[0].emails == ["marc@exemple.fr"]
    assert contacts[0].org == "Vitagro"


def test_contact_without_a_name_falls_back_to_a_placeholder():
    transport = FakeTransport(always(Response(200, {"connections": [person(names=[])]})))
    provider = GooglePeopleProvider(transport, token)
    contacts = run(provider.list_contacts())
    assert contacts[0].display_name == "(sans nom)"


def test_pagination_is_followed():
    def handler(_req, index):
        if index == 0:
            return Response(200, {"connections": [person(resourceName="a")], "nextPageToken": "p2"})
        return Response(200, {"connections": [person(resourceName="b")]})

    transport = FakeTransport(handler)
    provider = GooglePeopleProvider(transport, token)
    contacts = run(provider.list_contacts())
    assert [c.id for c in contacts] == ["a", "b"]
    assert len(transport.requests) == 2
