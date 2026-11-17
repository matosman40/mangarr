"""Catalogue local des sources : titres disponibles sur chaque site (lu une fois, mis à jour en arrière-plan).

Sert à ne proposer dans « Découvrir » que des mangas réellement disponibles sur vos sources, et à retrouver
instantanément le bon slug lors d'un ajout.
"""
import threading
import time

from mangarr.core.net import ChapterMissing, FetchError
from mangarr.sources import instantiate, searchable_names
from mangarr.titles import norm

MAX_PAGES = 150
PAUSE = 0.25                    # politesse entre deux pages d'un même site
STALE_SECONDS = 24 * 3600
INDEX_TTL = 60
SEED_QUERIES = "abcdefghijklmnopqrstuvwxyz0123456789"   # sites qui ne listent rien sans texte de recherche

LOCK = threading.Lock()
STATE = {"running": False, "source": None, "pages": 0, "titles": 0}


def _crawl(ctx, name):
    """(entrées, erreur) : parcourt le catalogue d'une source page par page."""
    src = instantiate(name, page_delay=0)
    seen, error, page = {}, None, 1
    while page <= MAX_PAGES:
        try:
            res = src.search(ctx.crawl_session, "", page)
        except (FetchError, ChapterMissing, NotImplementedError) as e:
            error = str(e) or type(e).__name__
            break
        except Exception as e:      # un site qui change de structure ne doit pas bloquer les autres
            error = f"erreur inattendue : {type(e).__name__}"
            break
        new = [r for r in res if r["slug"] not in seen]
        if not new:
            break
        seen.update({r["slug"]: r for r in new})
        STATE.update(pages=STATE["pages"] + 1, titles=STATE["titles"] + len(new))
        page += 1
        time.sleep(PAUSE)
    if len(seen) < 5 and error is None:        # rien listé sans texte : on balaie par lettres
        for ch in SEED_QUERIES:
            try:
                for r in src.search(ctx.crawl_session, ch, 1):
                    seen.setdefault(r["slug"], r)
            except (FetchError, ChapterMissing, NotImplementedError):
                break
            except Exception:
                break
            time.sleep(PAUSE)
    return list(seen.values()), error


def store(ctx, name, entries, error=None):
    """Remplace le catalogue d'une source. Une lecture vide ou en échec conserve l'ancien contenu."""
    if entries:
        rows = [(name, e["slug"], e["title"], norm(e["title"]), e.get("cover")) for e in entries if norm(e["title"])]
        ctx.db.execute("DELETE FROM catalog WHERE source=?", (name,))
        ctx.db.executemany("INSERT OR REPLACE INTO catalog(source, slug, title, norm, cover) VALUES (?,?,?,?,?)", rows)
    count = ctx.db.one("SELECT COUNT(*) AS n FROM catalog WHERE source=?", (name,))["n"]
    ctx.db.execute("INSERT INTO catalog_meta(source, refreshed, count, error) VALUES (?,?,?,?) "
                   "ON CONFLICT(source) DO UPDATE SET refreshed=excluded.refreshed, count=excluded.count, "
                   "error=excluded.error", (name, time.time(), count, error))
    ctx._cat_index = None


def refresh(ctx, only=None, stale_only=False):
    """Met à jour les catalogues (synchrone). Retourne le nombre de sources traitées."""
    if not LOCK.acquire(blocking=False):
        return 0
    done = 0
    try:
        STATE.update(running=True, pages=0, titles=0)
        meta = {r["source"]: r for r in ctx.db.query("SELECT * FROM catalog_meta")}
        for name in ([only] if only else searchable_names()):
            m = meta.get(name)
            if stale_only and m and m["count"] and time.time() - (m["refreshed"] or 0) < STALE_SECONDS:
                continue
            STATE["source"] = name
            entries, error = _crawl(ctx, name)
            store(ctx, name, entries, error)
            ctx.repo.add_log("info" if entries else "warn",
                             f"Catalogue {name} : {len(entries)} titre(s)" + (f" ({error})" if error else ""))
            done += 1
    finally:
        STATE.update(running=False, source=None)
        LOCK.release()
    return done


def refresh_async(ctx, only=None, stale_only=False):
    """Lance la mise à jour en arrière-plan (sans effet si déjà en cours). Retourne True si lancée."""
    if LOCK.locked():
        return False
    threading.Thread(target=refresh, args=(ctx, only, stale_only), daemon=True, name="catalog-refresh").start()
    return True


def status(ctx):
    rows = {r["source"]: r for r in ctx.db.query("SELECT * FROM catalog_meta")}
    return [{"source": n, "count": rows.get(n, {}).get("count", 0), "refreshed": rows.get(n, {}).get("refreshed"),
             "error": rows.get(n, {}).get("error")} for n in searchable_names()]


def total(ctx):
    names = searchable_names()
    if not names:
        return 0
    marks = ",".join("?" * len(names))
    return ctx.db.one(f"SELECT COUNT(*) AS n FROM catalog WHERE source IN ({marks})", names)["n"]


def index(ctx):
    """{titre normalisé -> (source, slug, titre du site, jaquette)} ; en cas d'égalité, la 1re source gagne."""
    cached = getattr(ctx, "_cat_index", None)
    if cached and time.time() - cached[0] < INDEX_TTL:
        return cached[1]
    order = {n: i for i, n in enumerate(searchable_names())}
    idx = {}
    for r in sorted(ctx.db.query("SELECT * FROM catalog"), key=lambda r: order.get(r["source"], 99)):
        if r["source"] in order:
            idx.setdefault(r["norm"], (r["source"], r["slug"], r["title"], r["cover"]))
    ctx._cat_index = (time.time(), idx)
    return idx


def match(ctx, titles):
    """(source, slug, titre du site) du premier titre connu qui existe dans un catalogue, ou None."""
    idx = index(ctx)
    for t in titles:
        hit = idx.get(norm(t))
        if hit:
            return hit[0], hit[1], hit[2]
    return None
