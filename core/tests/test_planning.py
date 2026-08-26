import pytest

from core.planning import PlanError, Task, topological_layers


def scenario_rdv_plus_email() -> list[Task]:
    """« Ajoute un RDV demain 10 h avec Marc et envoie-lui une confirmation »"""
    return [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "calendar.detect_conflicts", {"start": "2026-08-27T10:00:00+02:00"}),
        Task("T3", "calendar.create_event", {"title": "Point"}, depends_on=("T1", "T2")),
        Task("T4", "mail.draft", {"to": "{{T1.email}}"}, depends_on=("T1", "T3")),
        Task("T5", "mail.send", {"draft_id": "{{T4.id}}"}, depends_on=("T4",)),
    ]


def test_independent_tasks_share_a_layer():
    layers = topological_layers(scenario_rdv_plus_email())
    assert [t.id for t in layers[0]] == ["T1", "T2"]
    assert [[t.id for t in layer] for layer in layers[1:]] == [["T3"], ["T4"], ["T5"]]


def test_priority_orders_within_a_layer():
    tasks = [
        Task("low", "web.search", priority=0),
        Task("high", "memory.recall", priority=5),
    ]
    assert [t.id for t in topological_layers(tasks)[0]] == ["high", "low"]


def test_cycle_is_refused():
    tasks = [
        Task("A", "web.search", depends_on=("B",)),
        Task("B", "web.search", depends_on=("A",)),
    ]
    with pytest.raises(PlanError, match="cycle"):
        topological_layers(tasks)


def test_unknown_dependency_is_refused():
    with pytest.raises(PlanError, match="dépendance inconnue"):
        topological_layers([Task("A", "web.search", depends_on=("ghost",))])


def test_duplicate_task_id_is_refused():
    with pytest.raises(PlanError, match="dupliqué"):
        topological_layers([Task("A", "web.search"), Task("A", "web.search")])


def test_reference_substitution():
    task = Task("T4", "mail.draft", {"to": "{{T1.emails.0}}", "subject": "Point"})
    results = {"T1": {"emails": {"0": "marc@exemple.fr"}}}
    assert task.resolve_args(results)["to"] == "marc@exemple.fr"


def test_unresolved_reference_raises_instead_of_sending_to_nobody():
    task = Task("T4", "mail.draft", {"to": "{{T1.email}}"})
    with pytest.raises(PlanError, match="inconnue"):
        task.resolve_args({})


def test_nested_references_are_substituted():
    task = Task("T", "calendar.create_event", {"attendees": ["{{T1.email}}", "fixe@x.fr"]})
    resolved = task.resolve_args({"T1": {"email": "marc@exemple.fr"}})
    assert resolved["attendees"] == ["marc@exemple.fr", "fixe@x.fr"]
