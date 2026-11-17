# Mangarr

Télécharge automatiquement les nouveaux chapitres de mangas, les range en CBZ (avec `ComicInfo.xml`),
puis les assemble en tomes grâce aux données de volumes de MangaDex. *(Interface web : phases suivantes.)*

## État actuel (phase 1 : fondations)

Bibliothèque Python + base SQLite + planificateur + ligne de commande.

```bash
pip install -r requirements-dev.txt
export MANGARR_LIBRARY=./library MANGARR_CONFIG=./config

python -m mangarr add One_Piece --source lelscanfr --start 1100 --param slug=one-piece
python -m mangarr list
python -m mangarr sources
python -m mangarr run --dry-run          # ne télécharge rien
python -m mangarr run --only One_Piece -v
python -m mangarr daemon                 # planificateur seul
python -m pytest
```

Variables : voir [.env.example](.env.example). Volumes : `/library` (tomes, et chapitres en cours dans
`/library/_chapters`), `/config` (base, cache, jaquettes, plugins, exports Patreon).

## Sources

`pattern`, `html`, `lelscans`, `lelscanfr`, `animesama`, `patreon`, plus les plugins `.py` de
`/config/plugins` (voir [plugins_example](plugins_example/)) et les sources « formulaire » stockées en base.

Patreon bloque les requêtes serveur : Mangarr ne tente pas de contourner cette protection. Il lit des
exports JSON faits depuis votre navigateur, placés dans `/config/patreon/<id_du_post>.json`.

## English (short)

Mangarr downloads new manga chapters into CBZ files and merges them into volumes using MangaDex volume data.
Phase 1 provides the core library, SQLite storage, scheduler and CLI (`python -m mangarr`). The web UI is
coming next. Licensed under MIT.
