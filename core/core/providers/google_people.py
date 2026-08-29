"""Google People — source réelle de `core/contacts.py`.

`contacts.py` note déjà le score, décide de la désambiguïsation et formate la
question : ce module se contente de lui fournir de vrais `Contact` au lieu
d'une liste passée à la main, en traduisant la réponse de l'API People.

**Pas de recherche côté serveur qui accepte un nom partiel.** L'endpoint
`people:searchContacts` existe mais exige un index préalablement construit
(`otherContacts`/warm-up) et ne couvre pas tous les contacts d'un compte. On
liste donc systématiquement `connections` en entier et on laisse
`contacts.resolve` scorer localement — cohérent avec la doc de `resolve()`
elle-même : le score doit toujours regarder l'ensemble des candidats, jamais
un sous-ensemble déjà filtré par du texte.
"""

from __future__ import annotations

from ..contacts import Contact
from ..integrations.http import Transport, request_with_retry

API_ROOT = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations"


class GooglePeopleProvider:
    def __init__(self, transport: Transport, access_token_provider) -> None:
        self._transport = transport
        # Callable async : le jeton est résolu à chaque appel, comme pour Calendar/Gmail.
        self._access_token = access_token_provider

    async def _call(self, path: str, *, params: dict):
        token = await self._access_token()
        response = await request_with_retry(
            self._transport,
            "GET",
            f"{API_ROOT}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        return response.json()

    async def list_contacts(self) -> list[Contact]:
        contacts: list[Contact] = []
        page_token = None
        while True:
            params = {"personFields": PERSON_FIELDS, "pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            body = await self._call("/people/me/connections", params=params)
            for person in body.get("connections", []):
                contacts.append(_to_contact(person))
            page_token = body.get("nextPageToken")
            if not page_token:
                return contacts


def _to_contact(person: dict) -> Contact:
    names = person.get("names") or []
    orgs = person.get("organizations") or []
    return Contact(
        id=person.get("resourceName", ""),
        display_name=names[0]["displayName"] if names else "(sans nom)",
        emails=[e["value"] for e in person.get("emailAddresses", []) if e.get("value")],
        phones=[p["value"] for p in person.get("phoneNumbers", []) if p.get("value")],
        org=orgs[0].get("name") if orgs else None,
    )
