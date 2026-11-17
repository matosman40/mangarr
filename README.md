# Mangarr

Application auto-hébergée qui **télécharge les nouveaux chapitres de vos mangas**, les range en CBZ
(avec `ComicInfo.xml`), puis les **assemble en tomes** grâce aux données de volumes de MangaDex.
Interface web, planificateur intégré, notifications, ligne de commande. Un seul conteneur Docker.

> Mangarr est un outil de gestion de fichiers personnels. Vous êtes responsable du respect du droit
> d'auteur et des conditions d'utilisation des sites que vous configurez.

## Fonctionnalités

- Grille des mangas avec jaquettes, détail, historique, journaux.
- Ajout guidé : recherche MangaDex (titre, jaquette, tomes), choix de la source, bouton « tester l'adresse ».
- Sources : `pattern`, `html`, `lelscans`, `lelscanfr`, `animesama`, `patreon`, sources « formulaire »
  créées dans l'interface, et plugins Python pour les sites complexes.
- Vérification automatique (intervalle réglable) et à la demande, par manga ou pour tous.
- Tomes assemblés uniquement si tout est cohérent ; les chapitres ne sont supprimés qu'après vérification du tome.
- Jaquettes écrites dans les dossiers (`cover.jpg`) pour Komga, Kavita ou Plex.
- Notifications Discord, Telegram, ntfy ; rafraîchissement automatique de Komga et Kavita.
- Import de la configuration de l'ancien script `manga_dl.py`.
- Mode ligne de commande : `python -m mangarr ...`.

## Installation (Docker / CasaOS)

L'image est publiée par GitHub Actions : `ghcr.io/matosman40/mangarr:latest`
(si le paquet est privé : rendez-le public dans les réglages du paquet GitHub, ou `docker login ghcr.io`).

```yaml
services:
  mangarr:
    image: ghcr.io/matosman40/mangarr:latest
    container_name: mangarr
    restart: unless-stopped
    user: "1000:1000"            # UID:GID propriétaire de vos dossiers
    ports: ["8080:8080"]
    environment:
      TZ: Europe/Paris
      MANGARR_PASSWORD: "changez-moi"
      MANGARR_CHECK_INTERVAL: "360"
    volumes:
      - ./library:/library
      - ./config:/config
```

Dans CasaOS : *Applications > Installer une application personnalisée > Importer* un compose, ou saisir
l'image, le port 8080 et les deux volumes. Ouvrez ensuite `http://<serveur>:8080`.

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MANGARR_PASSWORD` | *(vide)* | Mot de passe de l'interface. **Vide = interface ouverte à tous** (réseau de confiance uniquement). |
| `MANGARR_PORT` | `8080` | Port de l'interface. |
| `MANGARR_CHECK_INTERVAL` | `360` | Minutes entre deux vérifications (`0` = désactivé). Modifiable dans l'interface. |
| `TZ` | `UTC` | Fuseau horaire. |
| `MANGARR_LIBRARY` | `/library` | Bibliothèque : tomes dans `/library/<manga>`. |
| `MANGARR_CHAPTERS` | `/library/_chapters` | Chapitres en cours. |
| `MANGARR_CONFIG` | `/config` | Base SQLite, cache, jaquettes, plugins, exports Patreon. |
| `MANGARR_PAGE_DELAY` / `MANGARR_CHAPTER_DELAY` | `0.3` / `2.0` | Pauses (secondes) entre pages / chapitres. |

Les dossiers par manga (chapitres, tomes) se règlent aussi individuellement dans le formulaire du manga.
Le conteneur tourne sans droits root (UID/GID 1000 par défaut) : les dossiers montés doivent lui être accessibles en écriture.

## Utilisation

1. **Ajouter** : menu *Ajouter*, recherchez le manga sur MangaDex, choisissez la source, renseignez ses champs,
   cliquez *Tester l'adresse* (nombre d'images trouvées ou erreur explicite), puis *Ajouter*.
2. **Vérifier** : *Tout vérifier* en haut, ou *Vérifier maintenant* sur la page d'un manga.
3. **Suivre** : page *Journaux* (journal en direct, derniers téléchargements, intervalle automatique).

### Règles des tomes

- Les volumes viennent de MangaDex (cache 12 h, repli sur le cache si l'API est KO).
- Un tome n'est assemblé que si : aucun trou dans les chapitres entiers, tous les chapitres sont présents,
  et les chapitres se raccordent à ceux des tomes voisins (option `tome_strict`, activée par défaut).
  Les chapitres décimaux sont ignorés.
- Le CBZ est écrit en `.part`, vérifié (archive lisible, nombre de pages exact), puis renommé ; **seulement ensuite**
  les chapitres sont supprimés. Le dernier chapitre téléchargé est mémorisé en base.

### Ajouter un site

- **Sans code** : *Sources > Nouvelle source*. Type `html` (page par chapitre : modèle d'adresse + expression
  pour trouver les images) ou `pattern` (images en `.../chapitre/page.jpg`). Un bouton teste la source.
- **Plugin Python** : déposez un `.py` dans `/config/plugins`, puis *Sources > Recharger les plugins*.
  Voir [plugins_example/monsite.py](plugins_example/monsite.py).
- Un domaine qui change (`anime-sama.fr`, `lelscanfr.com`…) se règle par manga (champ « Domaine »).

### Patreon

Patreon bloque les requêtes venant d'un serveur (HTTP 403, Cloudflare). **Mangarr ne tente pas de contourner
cette protection** et ne stocke aucun cookie : il lit des exports JSON faits depuis *votre* navigateur.

1. Ajoutez une série avec la source `patreon` : une ligne par tome, `numéro adresse-du-post` (un post = un tome).
2. Page *Patreon* : elle liste les exports manquants ou expirés, avec leur date d'expiration
   (≈ 38 h pour `download_url`, 14 jours pour `image_urls.original`).
3. Dans votre navigateur connecté à Patreon, ouvrez la collection, collez dans la console le script
   [docs/patreon-export.js](docs/patreon-export.js) : il télécharge un `<id_du_post>.json` par post.
   (Pour un seul post : ouvrez le lien « page d'API » affiché sur la page Patreon et enregistrez-la en `.json`.)
4. Glissez-déposez les fichiers sur la page *Patreon*, puis lancez une vérification.

### Notifications et bibliothèques

*Réglages* : webhook Discord, bot Telegram, sujet ntfy ; Komga (clé d'API ou identifiant/mot de passe + id de
bibliothèque) et Kavita (clé d'API + id). Les clés sont stockées dans la base (`/config`) et jamais réaffichées.
Un message n'est envoyé que pour les nouveautés et les **nouvelles** erreurs (pas de répétition à chaque passage).
Plex : activez l'analyse automatique de la bibliothèque.

### Importer l'ancienne configuration

*Réglages > Importer*, ou en ligne de commande (simulation par défaut) :

```bash
docker exec -it mangarr python -m mangarr import /config/manga_dl.py            # simulation
docker exec -it mangarr python -m mangarr import /config/manga_dl.py --apply
```

Seule la liste `MANGAS` est lue (analyse statique : le fichier n'est **jamais exécuté**). Les chemins personnels
ne sont pas repris (sauf `--keep-folders`) ; `--chapters-root` reprend le dernier chapitre téléchargé de
`.mangadl_state.json`, `--patreon-dir` copie vos exports Patreon.

## Ligne de commande

```bash
python -m mangarr list | sources
python -m mangarr add Mon_Manga --source lelscanfr --start 1 --param slug=mon-manga
python -m mangarr run [--only NOM ...] [--max N] [--dry-run] [-v] [--no-tomes] [--tomes-only] [--keep-chapters]
python -m mangarr serve      # interface + planificateur (commande du conteneur)
python -m mangarr daemon     # planificateur seul
```

## Sécurité

- Définissez `MANGARR_PASSWORD` dès que l'interface est joignable par d'autres personnes que vous
  (cookie de session signé, `HttpOnly`, `SameSite=Lax`). Ne l'exposez pas sur Internet sans reverse proxy HTTPS.
- Les boutons de test et les sources font des requêtes HTTP depuis votre serveur vers des adresses que
  vous saisissez : réservez l'accès à des personnes de confiance.
- Aucun secret n'est dans le dépôt ; configuration, base et exports restent dans vos volumes (`.gitignore` strict).

## Développement

```bash
pip install -r requirements-dev.txt
python -m pytest        # faux serveurs HTTP locaux, dossiers temporaires, aucun accès réseau externe
```

Structure : `mangarr/core` (config, base, réseau, moteur), `mangarr/sources`, `mangarr/tomes`, `mangarr/web`,
`mangarr/cli.py`, `plugins_example/`, `tests/`.

## English (short)

Mangarr is a self-hosted web app that downloads new manga chapters into CBZ files and merges them into volumes
using MangaDex volume data. It has a web UI with covers, a built-in scheduler, a form-based source editor plus
Python plugins, Discord/Telegram/ntfy notifications, Komga/Kavita refresh, and a CLI (`python -m mangarr`).
Run the image `ghcr.io/matosman40/mangarr` with volumes `/library` and `/config`, set `MANGARR_PASSWORD`, and open
port 8080. Patreon blocks server requests; Mangarr never bypasses this and instead reads JSON exports made from
your own browser (see `docs/patreon-export.js`). The UI is currently French-only. MIT licensed.
