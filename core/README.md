# Nova Core

Backend agentique. Les clients (WhatsApp, Chrome, Desktop, Mobile) ne sont que des
transports vers ce Core : ils envoient du texte ou de l'audio, ils reçoivent du
texte et des événements. Toute la logique vit ici.

Spécification complète : [`../docs/nova-spec.html`](../docs/nova-spec.html).

## État d'avancement

Ce qui est implémenté et couvert par des tests :

| Module | Rôle |
|---|---|
| `core/planning.py` | Décomposition en DAG, couches parallélisables, résolution des références inter-tâches |
| `core/gate.py` | Confirmation Gate — politique fermée par défaut, expiration, classification des réponses |
| `core/contacts.py` | Résolution de contacts scorée, désambiguïsation obligatoire |
| `core/graph/executor.py` | Exécution parallèle par couche, suspension avant action sortante, reprise |
| `core/tools/registry.py` | Registre d'outils, résultats structurés, idempotence |
| `core/providers/calendar.py` | Interface Google/Outlook, détection de conflits, créneaux libres |
| `core/security/crypto.py` | Chiffrement AES-GCM des tokens OAuth, AAD anti-substitution |
| `core/api/webhooks.py` | Signature HMAC, anti-rejeu, déduplication, normalisation Evolution |

Ce qui reste à câbler, marqué `NotImplementedError` dans le code :

- `get_verified_channel` — appairage des numéros WhatsApp (table `channels`)
- `handle_message` — pipeline complet, à brancher sur ARQ
- Les providers Google et Microsoft réels (l'interface et le double de test existent)
- Les nœuds LLM du graphe (router Haiku, planner Opus)
- Migrations Alembic

## Démarrer

```bash
cd core
pip install -e ".[dev]"
pytest -q
```

Pile complète :

```bash
cp infra/.env.example infra/.env
python -m core.security.crypto --generate   # → NOVA_MASTER_KEY
docker compose -f infra/docker-compose.yml up
```

## Les trois invariants

Ils ne sont pas négociables — chacun correspond à une panne réelle de ce type de système.

**1. Le Confirmation Gate est fermé par défaut.** Un outil absent de `FREE_TOOLS`
est traité comme irréversible. Ajouter un outil sortant sans y penser ne peut pas
ouvrir de brèche silencieuse. Une réponse ambiguë (« ok mais change l'objet ») n'est
jamais un accord.

**2. Tout outil mutatif est idempotent.** La clé dérive de `task_id + tool + args`
triés. Un timeout réseau suivi d'un retry ne crée pas trois réunions identiques.

**3. Un contact ambigu bloque l'exécution.** Si le meilleur candidat score moins de
0.85, ou si l'écart avec le suivant est inférieur à 0.15, l'agent demande. Envoyer un
document au mauvais Marc est l'erreur la plus coûteuse que ce système puisse commettre.

## Tests

63 tests, aucune dépendance réseau — `InMemoryCalendar` et le registre d'outils
permettent d'exercer tout le graphe hors ligne.

Les cas qui comptent : `test_execution_suspends_before_sending` (rien ne part avant
validation), `test_completed_tasks_are_not_replayed_on_resume`, 
`test_blob_cannot_be_moved_to_another_user`, `test_adjacent_events_do_not_conflict`.
