from datetime import datetime, timedelta, timezone

from core.contacts import Contact, format_disambiguation, resolve

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

MARC_DUBOIS = Contact("c1", "Marc Dubois", ["marc@exemple.fr"], org="Exemple SA")
MARC_LEROY = Contact("c2", "Marc Leroy", ["m.leroy@autre.fr"], org="Autre SARL")
SOPHIE = Contact("c3", "Sophie Bernard", ["sophie@exemple.fr"], org="Exemple SA")


def test_single_match_resolves_cleanly():
    r = resolve("Marc", [MARC_DUBOIS, SOPHIE], now=NOW)
    assert not r.needs_disambiguation
    assert r.best is MARC_DUBOIS


def test_two_homonyms_force_a_question():
    """Le cœur du sujet : ne jamais choisir entre deux Marc tout seul."""
    r = resolve("Marc", [MARC_DUBOIS, MARC_LEROY], now=NOW)
    assert r.needs_disambiguation
    assert "écart" in r.reason
    assert len(r.options()) == 2


def test_full_name_disambiguates():
    r = resolve("Marc Dubois", [MARC_DUBOIS, MARC_LEROY], now=NOW)
    assert not r.needs_disambiguation
    assert r.best is MARC_DUBOIS


def test_hint_breaks_the_tie():
    r = resolve("Marc", [MARC_DUBOIS, MARC_LEROY], hint="Exemple SA", now=NOW)
    assert not r.needs_disambiguation
    assert r.best is MARC_DUBOIS


def test_explicit_email_is_never_ambiguous():
    r = resolve("marc@exemple.fr", [MARC_DUBOIS, MARC_LEROY], now=NOW)
    assert not r.needs_disambiguation
    assert r.best is MARC_DUBOIS


def test_accents_and_case_are_ignored():
    herve = Contact("c4", "Hervé Lémont", ["herve@x.fr"])
    r = resolve("herve lemont", [herve, SOPHIE], now=NOW)
    assert not r.needs_disambiguation
    assert r.best is herve


def test_no_match_asks_for_the_address():
    r = resolve("Xavier", [MARC_DUBOIS, SOPHIE], now=NOW)
    assert r.needs_disambiguation
    assert r.best is None
    assert "aucun contact" in format_disambiguation("Xavier", r)


def test_recency_alone_cannot_override_ambiguity():
    """Un échange récent départage, mais ne suffit jamais à trancher seul."""
    recent = Contact(
        "c5", "Marc Leroy", ["m.leroy@autre.fr"],
        last_interaction_at=NOW - timedelta(days=1),
    )
    r = resolve("Marc", [MARC_DUBOIS, recent], now=NOW)
    assert r.needs_disambiguation


def test_disambiguation_lists_at_most_three_options():
    many = [Contact(f"c{i}", "Marc Martin", [f"m{i}@x.fr"]) for i in range(6)]
    r = resolve("Marc", many, now=NOW)
    assert r.needs_disambiguation
    assert len(r.options()) == 3
    assert format_disambiguation("Marc", r).count("\n") == 3
