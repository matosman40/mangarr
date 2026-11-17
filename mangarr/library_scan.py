"""Détection des mangas déjà présents dans les dossiers (bibliothèque des tomes et chapitres en cours)."""
import os
import re
import time

CACHE_SECONDS = 30      # les dossiers d'un NAS sont lents à parcourir : un résultat récent suffit

ARCHIVES = (".cbz", ".cbr", ".zip", ".rar", ".pdf", ".epub", ".7z")
# « _T01 » (Mangarr), « Tome 1 », « T01 », « Vol. 12 », « v03 »… (pas à l'intérieur d'un mot)
VOL_RE = re.compile(r"(?<![A-Za-z])(?:tome|volume|vol|t|v)\.?[\s._-]*0*(\d{1,3})(?!\d)", re.I)


def volume_number(filename):
    """Numéro de tome déduit d'un nom de fichier d'archive, ou None."""
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in ARCHIVES:
        return None
    m = None
    for m in VOL_RE.finditer(stem):      # le dernier motif du nom est le plus fiable (« Nom T2 - Tome 05 »)
        pass
    return int(m.group(1)) if m else None


def _dirs(path):
    try:
        return [e for e in os.scandir(path) if e.is_dir() and not e.name.startswith((".", "_", "@", "#"))]
    except OSError:
        return []


def _archives(path):
    try:
        return [e.name for e in os.scandir(path) if e.is_file() and e.name.lower().endswith(ARCHIVES)]
    except OSError:
        return []


def scan(ctx, fresh=False):
    """Liste des séries détectées : [{name, path, tomes, files, chapters, tracked, manga_id}], triée par nom.

    Le parcours des dossiers est mis en cache 30 s ; `fresh=True` force une nouvelle lecture.
    """
    cached = getattr(ctx, "_scan_cache", None)
    if not fresh and cached and time.time() - cached[0] < CACHE_SECONDS:
        raw = cached[1]
    else:
        raw = _scan_folders(ctx)
        ctx._scan_cache = (time.time(), raw)
    tracked = {m["name"].casefold(): m["id"] for m in ctx.repo.list_mangas()}
    out = []
    for item in raw:
        item = dict(item)
        item["manga_id"] = tracked.get(item["name"].casefold())
        item["tracked"] = item["manga_id"] is not None
        out.append(item)
    return out


def _scan_folders(ctx):
    s = ctx.settings
    series = {}
    chapters_root = os.path.abspath(s.chapters_dir)
    for e in _dirs(s.library_dir):
        if os.path.abspath(e.path) == chapters_root:
            continue
        files = _archives(e.path)
        if not files:
            continue
        tomes = sorted({v for f in files if (v := volume_number(f)) is not None})
        series[e.name.casefold()] = {"name": e.name, "path": e.path, "tomes": tomes, "files": len(files),
                                     "chapters": []}
    for e in _dirs(s.chapters_dir):
        pat = re.compile(rf"^{re.escape(e.name)}_(\d+)\.cbz$")
        chaps = sorted(int(m.group(1)) for f in _archives(e.path) if (m := pat.match(f)))
        if not chaps:
            continue
        item = series.setdefault(e.name.casefold(), {"name": e.name, "path": None, "tomes": [], "files": 0,
                                                    "chapters": []})
        item["chapters"] = chaps
    return [item for _, item in sorted(series.items())]


def index_by_norm(series, norm):
    """{titre normalisé -> série} pour les comparaisons rapides (badge « en bibliothèque »)."""
    return {norm(x["name"]): x for x in series if norm(x["name"])}
