# my-agent — Nova

Nova, assistant exécutif personnel polyvalent.

## Contenu

- `web/index.html` — page de présentation autonome de Nova (aucune dépendance, s'ouvre
  directement dans un navigateur ; seules les polices Google Fonts sont chargées en ligne).

## Utilisation

Ouvrir `web/index.html` dans un navigateur, ou servir le dossier :

```
python3 -m http.server 8000 --directory web
```

## Documentation

- `docs/nova-spec.html` — spécification technique complète et guide d'implémentation
  (architecture, modèle de données, catalogue d'outils, prompt système, workflows,
  plan de développement en 8 semaines).
