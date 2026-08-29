# my-agent — Nova

Nova, assistant exécutif personnel polyvalent.

## Contenu

- `web/index.html` — page de présentation autonome de Nova (aucune dépendance, s'ouvre
  directement dans un navigateur ; seules les polices Google Fonts sont chargées en ligne).
- `core/` — backend agentique (voir `core/README.md`).
- `app/` — client Android/iOS/Windows (voir `app/README.md`), contre le canal `app` du backend.
- `infra/` — Docker Compose et configuration de déploiement.

## Utilisation

Ouvrir `web/index.html` dans un navigateur, ou servir le dossier :

```
python3 -m http.server 8000 --directory web
```
