"""Ajout automatique d'un manga : source trouvée sur les sites, dossiers existants repris, jaquette."""
import re
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from mangarr import library_scan
from mangarr.core.net import ChapterMissing, FetchError
from mangarr.core.util import clean_name
from mangarr.sources import SOURCES, searchable_names
from mangarr.tomes import mangadex

THRESHOLD = 0.88            # similarité minimale entre un titre connu et celui d'un site / d'un dossier
SETUP_MESSAGE = "Source à configurer : aucun site n'a été trouvé automatiquement"
EXISTING_MESSAGE = "Dossier existant détecté : indiquez le chapitre de départ avant d'activer le suivi"
SETUP_MESSAGES = {SETUP_MESSAGE, EXISTING_MESSAGE}
MAX_TITLES = 4


def needs_setup(manga, state):
    """Manga créé automatiquement mais incomplet (suspendu avec un message de configuration)."""
    return (not manga["enabled"]) and (state or {}).get("last_message") in SETUP_MESSAGES


def norm(title):
    return re.sub(r"[\W_]+", "", (title or "").casefold())


def score(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def best_result(results, titles):
    """(score, résultat) du résultat le plus proche d'un des titres connus."""
    best = (0.0, None)
    for r in results:
        s = max((score(r["title"], t) for t in titles), default=0.0)
        if s > best[0]:
            best = (s, r)
    return best


# ------------------------------------------------------------------ sources
def _search_source(ctx, name, titles):
    cls = SOURCES[name]
    src = cls({**cls.defaults}, page_delay=0)
    for t in titles[:MAX_TITLES]:
        try:
            results = src.search(ctx.fast_session, t, 1)
        except (FetchError, ChapterMissing, NotImplementedError):
            continue
        except Exception:      # un site qui change de structure ne doit pas bloquer l'ajout
            continue
        s, r = best_result(results, titles)
        if r and s >= THRESHOLD:
            return name, s, r
    return name, 0.0, None


def find_source(ctx, titles):
    """(source, slug, titre du site, score) de la meilleure correspondance, ou None."""
    names = searchable_names()
    if not names or not titles:
        return None
    with ThreadPoolExecutor(max_workers=min(6, len(names))) as pool:
        found = [f for f in pool.map(lambda n: _search_source(ctx, n, titles), names) if f[2]]
    if not found:
        return None
    order = {n: i for i, n in enumerate(names)}
    name, s, r = sorted(found, key=lambda f: (-f[1], order[f[0]]))[0]
    return name, r["slug"], r["title"], s


# ------------------------------------------------------------------ dossiers existants
def match_local(ctx, titles, series=None):
    """Série déjà présente dans les dossiers (et pas encore suivie) qui correspond à un des titres."""
    best, best_s = None, 0.0
    for x in (series if series is not None else library_scan.scan(ctx)):
        if x["tracked"]:
            continue
        s = max((score(x["name"], t) for t in titles), default=0.0)
        if s > best_s:
            best, best_s = x, s
    return best if best_s >= THRESHOLD else None


def resume_chapter(ctx, md_id, name, local):
    """Dernier chapitre déjà possédé : chapitres en cours + chapitres des tomes présents (via MangaDex)."""
    last = max(local["chapters"]) if local["chapters"] else None
    if local["tomes"] and md_id:
        try:
            vols, _, _ = mangadex.volumes(ctx.fast_session, ctx.settings.cache_dir, md_id, name,
                                          {"mangadex_id": md_id, "mangadex_api": ctx.settings.mangadex_api})
        except FetchError:
            vols = {}
        owned = [c for v in local["tomes"] for c in vols.get(v, [])]
        if owned:
            last = max(last or 0, max(owned))
    return last


# ------------------------------------------------------------------ ajout
def unique_name(ctx, base):
    name, i = base, 2
    while ctx.repo.get_manga(name):
        name, i = f"{base}_{i}", i + 1
    return name


def existing_for(ctx, md_id):
    if not md_id:
        return None
    return next((m for m in ctx.repo.list_mangas() if m["params"].get("mangadex_id") == md_id), None)


def auto_add(ctx, item, local=None):
    """Ajoute le manga `item` (fiche MangaDex normalisée ; id peut être None). Retourne un dict de résultat.

    - source cherchée sur les sites ; sinon manga créé suspendu, à configurer via Modifier ;
    - dossier déjà présent : son nom est repris et le suivi continue après le dernier chapitre possédé
      (si ce chapitre est inconnu, le manga reste suspendu : on ne retélécharge jamais tout par erreur).
    """
    md_id, title = item.get("id"), item["title"]
    already = existing_for(ctx, md_id)
    if already:
        return {"id": already["id"], "name": already["name"], "created": False, "found": already["enabled"],
                "source": already["source"], "slug": already["params"].get("slug", ""), "resume": None,
                "local": False}
    titles = list(dict.fromkeys([title] + list(item.get("alt_titles") or [])))
    if local is None:
        local = match_local(ctx, titles)
    match = find_source(ctx, titles)
    params = {}
    if md_id:
        params.update(mangadex_id=md_id, mangadex_title=title)
    if match:
        source, slug, _, _ = match
        params["slug"] = slug
    else:
        names = searchable_names()
        source = names[0] if names else "pattern"

    start, resume, message = 1, None, None
    if local:
        name = local["name"] if not ctx.repo.get_manga(local["name"]) else unique_name(ctx, local["name"])
        resume = resume_chapter(ctx, md_id, name, local)
        if resume is None:
            message = EXISTING_MESSAGE
    else:
        name = unique_name(ctx, clean_name(title) or "Manga")
        if ctx.repo.get_setting("auto_add", {}).get("start") == "latest" and str(item.get("last_chapter") or "").isdigit():
            start = max(1, int(item["last_chapter"]))
    if not match and message is None:
        message = SETUP_MESSAGE
    enabled = message is None
    mid = ctx.repo.add_manga(name, source, params, start_chap=start, enabled=enabled, tomes_enabled=True)
    if resume:
        ctx.repo.save_last_chapter(mid, resume)      # le suivi reprend à resume + 1
    if message:
        ctx.repo.set_status(mid, "error", message)
    detail = (f"source {source}, slug {params['slug']}" if match else "aucune source trouvée")
    if local:
        detail += f" ; dossier existant repris" + (f", dernier chapitre {resume}" if resume else ", chapitre inconnu")
    ctx.repo.add_log("info" if enabled else "warn", f"Ajout automatique de « {name} » : {detail}", mid)
    try:
        from mangarr.tomes.covers import store_md_cover
        store_md_cover(ctx, mid, item.get("cover_url") or "")
    except Exception as e:     # la jaquette est facultative
        ctx.repo.add_log("warn", f"jaquette de {name} : {type(e).__name__}", mid)
    return {"id": mid, "name": name, "created": True, "found": bool(match), "source": source,
            "slug": params.get("slug", ""), "resume": resume, "local": bool(local), "enabled": enabled}


def adopt_folder(ctx, series, api):
    """Suit un dossier déjà présent : cherche le manga sur MangaDex par son nom, puis ajout automatique."""
    item = {"id": None, "title": series["name"], "alt_titles": [], "cover_url": None, "last_chapter": None}
    try:
        found, _ = mangadex.discover(ctx.fast_session, api, q=series["name"], order="relevance", limit=5)
    except Exception:
        found = []
    best = (0.0, None)
    for it in found:
        s = max((score(t, series["name"]) for t in [it["title"]] + it["alt_titles"]), default=0.0)
        if s > best[0]:
            best = (s, it)
    if best[1] and best[0] >= THRESHOLD:
        item = best[1]
    return auto_add(ctx, item, local=series)


def describe(res):
    """(niveau, texte français) du résultat d'un ajout automatique."""
    if not res["created"]:
        return "ok", "Déjà suivi."
    if not res["enabled"]:
        if res["local"] and res["resume"] is None:
            return "warn", "Ajouté depuis votre dossier : indiquez le chapitre de départ (Modifier) avant d'activer le suivi."
        return "warn", "Ajouté, mais aucun site trouvé automatiquement : choisissez la source (Modifier)."
    if res["local"]:
        return "ok", f"Dossier existant repris : suite après le chapitre {res['resume']} ({res['source']})."
    return "ok", f"Ajouté — source {res['source']}."
