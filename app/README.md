# Nova — client Android/iOS/Windows/Web

Un seul écran de discussion, contre le canal `app` de Nova
(`core/api/app_channel.py`) : appairage par code à 6 chiffres, puis échange
synchrone (chaque message attend sa réponse sur la même requête HTTP — pas de
notifications push, pas d'historique côté serveur).

## Pourquoi il n'y a pas de dossier `android/`, `ios/`, `windows/` ou `web/` ici

Ce dépôt ne contient que le code Dart (`lib/main.dart`) et `pubspec.yaml`.
`.github/workflows/build-app.yml` fait tourner `flutter create --platforms=...`
à chaque build pour régénérer le squelette natif (Gradle, Xcode, CMake) à
partir de la version de Flutter du moment, plutôt que de committer une copie
figée qui dérive silencieusement. Quatre jobs, un par plateforme :

- **Android** → `nova-android` (un `.apk`, installable directement — active
  « Sources inconnues » sur l'appareil, aucun compte Google Play requis).
- **Windows** → `nova-windows` (le dossier `Release/` de
  `flutter build windows`, avec l'exécutable dedans).
- **iOS** → `nova-ios-unsigned` (`Runner.app`, **non signé** : `--no-codesign`
  faute de compte Apple Developer/certificat de signature. Utilisable dans le
  Simulateur iOS ; installer sur un vrai iPhone demande de le signer avec un
  compte Apple Developer — impossible à produire sans lui).
- **Web** → `nova-web` (un dossier statique, `flutter build web`) — à héberger
  n'importe où (Vercel, Netlify, un simple `python -m http.server`). Son
  origine diffère forcément de celle de l'API : `core/api/main.py` autorise
  déjà `NOVA_CORS_ORIGINS` (par défaut `*`, restreignable) pour que le
  navigateur ne bloque pas les réponses de `/app/pair`/`/app/messages`.

Récupère les builds depuis l'onglet *Actions* du dépôt, sur le run du
workflow `build-app`.

## Développement local

Nécessite le SDK Flutter (`flutter --version`) :

```bash
cd app
flutter create --platforms=android,ios,windows,web .   # une seule fois
flutter pub get
flutter run                                             # appareil/émulateur connecté, ou -d chrome
```

## Utilisation

1. Sur le serveur Nova, un administrateur émet un code d'appairage :
   `POST /admin/pairing-code?user_id=...&key=...` (voir `core/README.md`).
2. Dans l'app, au premier lancement : adresse du serveur (`https://...`, sans
   `/` final) puis le code à 6 chiffres reçu à l'étape 1.
3. Le jeton renvoyé par `/app/pair` est stocké localement (`shared_preferences`)
   — jamais rejoué en clair côté serveur, qui n'en garde que le hash.

## Limites connues (v0.1)

- **Pas d'historique serveur.** Les messages affichés ne survivent qu'à la
  session de l'app ; fermer et rouvrir l'app repart d'un écran vide, même si
  la conversation continue côté serveur (le fil WhatsApp/`app` reste le même).
- **Pas de déconnexion serveur.** « Se déconnecter » efface le jeton local
  uniquement — `PostgresDeviceTokenStore.revoke` existe côté serveur mais
  n'est pas encore exposé par une route.
- **iOS non signé.** Voir plus haut.
