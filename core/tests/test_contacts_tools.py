import asyncio

from core.contacts import Contact
from core.tools.contacts_tools import register_contacts_tools
from core.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    def __init__(self, contacts: list[Contact]) -> None:
        self._contacts = contacts

    async def list_contacts(self) -> list[Contact]:
        return self._contacts


MARC = Contact("c1", "Marc Dubois", emails=["marc@exemple.fr"], org="Vitagro")
MARCO = Contact("c2", "Marco Neri", emails=["marco@exemple.it"])


def registry_with(contacts: list[Contact]) -> ToolRegistry:
    registry = ToolRegistry()
    register_contacts_tools(registry, FakeProvider(contacts))
    return registry


def test_resolve_returns_the_best_match_unambiguously():
    registry = registry_with([MARC])
    result = run(registry.call("contacts.resolve", {"query": "Marc"})).unwrap()
    assert result["needs_disambiguation"] is False
    assert result["best"]["display_name"] == "Marc Dubois"


def test_resolve_flags_ambiguous_candidates():
    """`best` reste le candidat le mieux classé, mais `needs_disambiguation` dit
    à l'appelant de ne pas s'y fier aveuglément — c'est lui qu'il faut vérifier."""
    registry = registry_with([MARC, MARCO])
    result = run(registry.call("contacts.resolve", {"query": "Mar"})).unwrap()
    assert result["needs_disambiguation"] is True
    assert len(result["options"]) == 2


def test_get_returns_none_for_an_unknown_id():
    registry = registry_with([MARC])
    assert run(registry.call("contacts.get", {"contact_id": "ghost"})).unwrap() is None


def test_get_returns_the_matching_contact():
    registry = registry_with([MARC])
    result = run(registry.call("contacts.get", {"contact_id": "c1"})).unwrap()
    assert result["display_name"] == "Marc Dubois"
    assert result["org"] == "Vitagro"
