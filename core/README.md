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
| `core/providers/google_calendar.py` | Provider Google réel : idempotence par id dérivé, sync incrémental, journées entières |
| `core/integrations/google_oauth.py` | Flow OAuth, rafraîchissement, détection de révocation |
| `core/integrations/http.py` | Retry avec backoff, `Retry-After`, quotas Google (403 = 429) |
| `core/security/crypto.py` | Chiffrement AES-GCM des tokens OAuth, AAD anti-substitution |
| `core/api/webhooks.py` | Signature HMAC, anti-rejeu, déduplication, normalisation Evolution |
| `core/api/oauth.py` | State signé anti-CSRF, callback, refus de confusion de compte |
| `core/store.py` | Store de jetons chiffré, expiration en clair |
| `core/channels.py` | Appairage par code à 6 chiffres, usage unique, anti-force brute |
| `core/pipeline.py` | Orchestration complète : transcription, routage, plan, gate, reprise |
| `core/approvals.py` | Validations en attente, une par fil, expiration à 30 min |
| `core/messaging.py` | Formatage WhatsApp, découpage 4096, envoi Evolution |
| `core/workers.py` | Worker ARQ, files à priorités, verrous par ressource, cron |
| `core/llm.py` | Router (Haiku 4.5), Planner et Responder (Opus 5) — validation du plan, refus |
| `core/prompts.py` | Prompt système et consignes par nœud, avec point de cache |
| `core/db/models.py` | Schéma relationnel — contraintes plutôt que conventions |
| `core/db/repositories.py` | Registres durables : canaux, validations, dédup, jetons |
| `core/db/session.py` | Moteur, sessions, portée transactionnelle |
| `alembic/` | Migrations — `alembic upgrade head` |
| `core/api/main.py` | Gateway FastAPI : webhook WhatsApp sur les registres Postgres, mise en file ARQ, flows OAuth Google et Microsoft |
| `core/tools/calendar_tools.py` | Outils `calendar.*`, liés à un provider Google ou Microsoft selon la configuration |
| `core/providers/gmail.py` | Provider Gmail réel : brouillon puis envoi, dédoublonnage applicatif faute d'id imposable côté Google |
| `core/tools/mail_tools.py` | Outils `mail.draft`/`mail.send` liés à ce provider |
| `core/providers/outlook_calendar.py` | Provider Microsoft Graph réel pour `calendar.*` — alternatif à Google, même dédoublonnage applicatif |
| `core/api/main.py` | Gateway FastAPI : webhook WhatsApp sur les registres Postgres, mise en file ARQ, flow OAuth Google |
| `core/tools/calendar_tools.py` | Outils `calendar.*` liés à un provider Google |
| `core/providers/google_people.py` | Provider People réel — fournit de vrais candidats à `core/contacts.py` |
| `core/tools/contacts_tools.py` | Outils `contacts.resolve`/`contacts.get` liés à ce provider |

Ce qui reste à câbler :

- Le déploiement réel : premier appel Anthropic facturé, instance WhatsApp
- People API (`contacts.resolve` sur de vraies données) — le prompt du
  planificateur le référence déjà, mais l'intégration n'existe pas : un plan
  qui l'invoque le voit écarté comme outil inconnu
- Le courrier Microsoft (Outlook Mail) — seul Calendar a un équivalent
  Microsoft pour l'instant ; `mail.*` reste Gmail uniquement
- Gmail (`mail.draft`/`mail.send`) — le prompt du planificateur le référence
  déjà, mais l'intégration n'existe pas : un plan qui l'invoque le voit
  écarté comme outil inconnu
- `contacts.recent_interactions` — nécessiterait de miner l'historique des
  messages, pas le carnet d'adresses ; fonctionnalité distincte, non construite
- Le provider Microsoft Graph (Google est fait ; l'interface est partagée)

## Le worker en production

`handle_message_job` (`core/workers.py`) construit un `Pipeline` par message,
pas un pipeline partagé au démarrage : ses outils calendrier sont fermés sur
les identifiants Google de l'utilisateur du message (`ensure_fresh` + le
`CredentialStore` chiffré), donc un pipeline construit une fois pour tout le
processus ferait fuiter l'agenda d'un utilisateur vers un autre. Ce qui *est*
partagé entre jobs, construit une fois par `startup()` : le client Anthropic,
le moteur DB, le transport HTTP, l'`EvolutionSender`.

L'approbation en attente (`core/approvals.py`) est portée par
`PostgresApprovalRegistry`, pas le registre en mémoire : deux jobs ARQ sont
deux appels de fonction indépendants, potentiellement sur des workers
différents. Un « ok » qui ne retrouverait pas la validation posée par le
message précédent reviendrait à relancer une planification vide — exactement
le mode de panne que la persistance devait éliminer.

## Gmail

`core/providers/gmail.py` + `core/tools/mail_tools.py`, câblés dans le worker
juste après Calendar (`core/workers.py`), sur le même jeton Google — les
scopes `gmail.modify`/`gmail.send` sont déjà demandés par
`integrations/google_oauth.py`, donc pas de second flow OAuth ni de second
`CredentialStore`.

**Pas d'id imposable côté client, contrairement à Calendar.** L'API Gmail
génère toujours un nouvel identifiant sur `drafts.create` et `drafts.send` :
un retry après timeout enverrait donc un second email identique si rien ne
l'en empêchait. `PostgresIdempotencyStore` (`core/db/repositories.py`,
table `idempotency_records`) joue le rôle qu'un en-tête `Idempotency-Key`
jouerait si Gmail en proposait un — la clé dérivée par le registre d'outils
est vérifiée avant l'appel HTTP et enregistrée après.

Le planificateur chaîne toujours `mail.draft` (libre) puis `mail.send`
(gated, comme `calendar.delete_event`) : un envoi direct n'existe pas dans le
prompt. `contacts.resolve` (People API) n'a pas d'implémentation — un plan
qui l'invoque le voit écarté comme outil inconnu, donc les adresses email
doivent encore venir du message de l'utilisateur.

## Outlook (Microsoft Graph)

`core/providers/outlook_calendar.py`, alternatif à Google pour `calendar.*`
(`core/workers.py` choisit Google en priorité si les deux sont configurés —
jamais les deux à la fois pour un même utilisateur, puisque les outils
`calendar.*` sont un seul jeu de noms dans le registre). Flow OAuth séparé
(`core/integrations/microsoft_oauth.py`, `GET /oauth/microsoft/start` puis
`/oauth/microsoft/callback`), sur le tenant `common` (comptes personnels et
professionnels/scolaires) — mêmes garde-fous que Google : `key` en secret
d'opérateur sur `/start`, `state` signé et daté portant l'identité.

**Pas d'id imposable côté client non plus.** Microsoft Graph attribue toujours
lui-même l'id d'un événement créé, comme Gmail et contrairement à Google
Calendar : même `IdempotencyStore` (`core/idempotency.py`) que pour Gmail.

Pas d'équivalent Outlook Mail pour l'instant — `mail.*` reste Gmail uniquement.

## Flow OAuth Google

`GET /oauth/google/start?user_id=...&key=...` puis `GET /oauth/google/callback`.
Nova n'a pas de dashboard ni de login : `key` (comparée à
`NOVA_OAUTH_START_SECRET` à temps constant) tient lieu d'authentification sur
`/start` — sans elle, n'importe qui pourrait lier son propre compte Google à
l'identité d'un autre utilisateur Nova. Le `state` qui en ressort est signé
(`NOVA_OAUTH_STATE_SECRET`) et expire en 10 minutes ; c'est lui, pas
`/callback`, qui porte la garantie anti-confusion de compte. Variables
requises : voir `infra/.env.example`.

## Appairer un premier numéro WhatsApp

`POST /admin/pairing-code?user_id=...&key=...` — même schéma que
`/oauth/google/start` (secret d'opérateur `NOVA_ADMIN_SECRET`, comparaison à
temps constant). C'est le seul moyen d'obtenir un code : rien d'autre
n'appelle `ChannelRegistry.issue_code`, ni dashboard ni CLI. Envoyer le code
retourné depuis le numéro WhatsApp à appairer complète l'appairage via le
webhook (`_try_pairing` dans `core/api/main.py`).



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

## TLS

`caddy` termine le TLS devant `core` — c'est lui qui reçoit le webhook WhatsApp
et le callback OAuth, pas `evolution` ni `worker`. Configuration minimale
(`infra/Caddyfile`) :

```
{$NOVA_DOMAIN} {
	reverse_proxy core:8000
}
```

Caddy obtient et renouvelle le certificat Let's Encrypt automatiquement dès
que `NOVA_DOMAIN` résout vers ce serveur et que les ports 80/443 sont
ouverts — rien à générer à la main. `core` n'expose plus son port directement
(`expose: ["8000"]`, pas `ports:`) : tout passe par Caddy. `NOVA_DOMAIN` et
`PUBLIC_URL` portent le même hôte (l'un nu, l'autre avec le schéma) et
doivent rester synchronisés — voir `infra/.env.example`.

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

216 tests, aucune dépendance réseau ni base externe — `InMemoryCalendar` et le registre d'outils
200 tests, aucune dépendance réseau ni base externe — `InMemoryCalendar` et le registre d'outils
permettent d'exercer tout le graphe hors ligne.

`FakeTransport` (dans `tests/conftest.py`) rejoue des réponses HTTP scriptées et
enregistre ce qui a été envoyé : les scénarios Google — 429 puis succès, 410 sur
syncToken, 409 sur création rejouée — sont exercés sans réseau.

Les cas qui comptent : `test_execution_suspends_before_sending` (rien ne part avant
validation), `test_completed_tasks_are_not_replayed_on_resume`,
`test_blob_cannot_be_moved_to_another_user`, `test_adjacent_events_do_not_conflict`,
`test_conflict_on_retry_is_treated_as_success`, `test_refresh_preserves_the_refresh_token`,
`test_all_day_events_are_not_read_as_timestamps`, `test_callback_refuses_account_confusion`,
`test_brute_force_burns_the_code`, `test_tokens_are_never_written_in_clear`,
`test_ambiguous_reply_replans_instead_of_sending`, `test_expired_approval_is_not_honoured`,
`test_low_confidence_asks_to_repeat_instead_of_guessing`,
`test_planner_drops_hallucinated_tools`, `test_volatile_context_stays_out_of_the_cached_block`,
`test_migration_matches_the_models`.

## Les nœuds LLM

Deux modèles : Haiku 4.5 pour le routage (l'essentiel du trafic est trivial),
Opus 5 pour la planification et la réponse. Le découpage tient parce que le
routeur ne décide de rien d'irréversible — au pire il escalade vers Opus.

Deux garde-fous côté code, indépendants du modèle :

**Le plan est validé contre le registre d'outils.** Un nom d'outil halluciné est
écarté avant l'exécution, avec ses dépendances orphelines — pas découvert au
milieu d'un plan à moitié appliqué.

**Un refus est traité comme un refus.** `stop_reason == "refusal"` est vérifié
avant de toucher à `content` : lire le contenu d'une réponse déclinée donne une
chaîne vide ou un message d'excuse, que le pipeline prendrait pour un résultat.

Le prompt système est mis en cache (`cache_control`), le contexte volatil — date,
canal — reste en dehors du bloc caché : une date dans le préfixe l'invaliderait à
chaque tour.

Câblage :

```python
client = anthropic.AsyncAnthropic()
router, planner, responder = build_nodes(client, frozenset(registry.tools))
pipeline = Pipeline(registry, router, planner, responder, sender)
```

## Le provider Google en trois points non devinables

**Idempotence par l'identifiant.** L'API Calendar n'a pas d'en-tête d'idempotence.
On impose donc l'`id` de l'événement, dérivé de la clé — et un 409 « existe déjà »
est traité comme un succès, pas comme une erreur. Sans cela, un retry après timeout
remonte un échec pour un rendez-vous bel et bien créé.

**Le syncToken expire.** Google répond alors 410 GONE. Ne pas gérer ce cas fige la
synchronisation définitivement, sans erreur visible.

**403 ne veut pas dire « interdit ».** Sur dépassement de quota, Google renvoie
parfois 403 avec `reason: rateLimitExceeded` au lieu de 429. Le confondre avec un
refus de permission fait abandonner un appel qui aurait réussi au second essai.


## Persistance

Les registres en mémoire marchaient pour les tests et pas au-delà : un
redémarrage du worker perdait tous les appairages et toutes les validations en
attente. Les deux implémentations coexistent — `ChannelRegistry` en mémoire pour
les tests, `PostgresChannelRegistry` pour la production — derrière la même
interface.

Trois garanties sont exprimées comme des contraintes de base, pas comme des
conventions de code :

- **Une seule validation en attente par fil** (clé primaire sur `thread_id`).
  Une vérification applicative laisserait passer deux requêtes concurrentes.
- **Un canal unique par `(kind, external_id)`**. Un même numéro ne peut pas être
  appairé à deux utilisateurs.
- **Les jetons sont des `LargeBinary`**, jamais du texte : la colonne ne peut
  pas contenir un secret en clair par accident.

```bash
export DATABASE_URL=postgresql+asyncpg://nova:...@localhost/nova
alembic upgrade head
```

`create_all()` existe pour les tests et le prototypage uniquement — il ne sait
pas faire évoluer une base existante, et l'utiliser en production masquerait une
migration oubliée. Un test compare d'ailleurs les modèles à la migration et
échoue si l'un a bougé sans l'autre.
