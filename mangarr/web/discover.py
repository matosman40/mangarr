"""Page « Découvrir » : catalogue MangaDex filtrable (genres, type, statut, tri), jaquettes et synopsis."""
import hashlib
import os
import re
from urllib.parse import urlencode, urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from collections import OrderedDict

from mangarr import autoadd, library_scan, translate
from mangarr.core.net import MD_HEADERS, sniff_image_ext
from mangarr.tomes import mangadex

ALLOWED_HOSTS = {"uploads.mangadex.org"}      # seule destination possible du proxy (pas de SSRF)
ALLOWED_SCHEMES = {"https"}
COVER_PATH = re.compile(r"^/covers/[0-9a-fA-F-]{36}/[A-Za-z0-9._-]{1,120}$")
MAX_COVER = 5 * 1024 * 1024
MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

PAGE = 24
SYNOPSIS_SHORT = 240
UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
SYN_CACHE = OrderedDict()      # id -> (synopsis, langue) : évite de recharger l'API pour chaque traduction
SYN_MAX = 3000


def shorten(text):
    return (text[:SYNOPSIS_SHORT].rsplit(" ", 1)[0] + "…") if len(text) > SYNOPSIS_SHORT else text


def remember(it, key=None):
    key = key or it["id"]
    SYN_CACHE[key] = (it["synopsis"], it["synopsis_lang"])
    SYN_CACHE.move_to_end(key)
    while len(SYN_CACHE) > SYN_MAX:
        SYN_CACHE.popitem(last=False)


def register(app, ctx, render):
    repo = ctx.repo

    def api():
        return ctx.settings.mangadex_api

    def followed_ids():
        return {m["params"].get("mangadex_id") for m in repo.list_mangas() if m["params"].get("mangadex_id")}

    @app.get("/discover", response_class=HTMLResponse)
    async def discover_page(request: Request):
        tags, error = [], None
        try:
            tags = await run_in_threadpool(mangadex.tag_list, ctx.fast_session, api())
        except Exception as e:
            error = f"Liste des genres indisponible : {type(e).__name__}"
        return render(request, "discover.html", tags=tags, error=error, orders=mangadex.ORDERS,
                      types=mangadex.TYPES, statuses=mangadex.STATUSES, demos=mangadex.DEMOS)

    @app.get("/discover/results", response_class=HTMLResponse)
    async def discover_results(request: Request, q: str = "", origin: str = "", order: str = "auto",
                               offset: int = 0, fr: str = "", adult: str = "", any_tag: str = ""):
        qp = request.query_params
        tags, status, demo = qp.getlist("tag"), qp.getlist("status"), qp.getlist("demo")
        offset = max(0, min(offset, 9900))
        if order == "auto":      # une recherche par titre se trie par pertinence, le catalogue par dernières sorties
            order = "relevance" if q.strip() else "latestUploadedChapter"
        try:
            items, total = await run_in_threadpool(
                lambda: mangadex.discover(ctx.fast_session, api(), q=q.strip(), tags=tags, all_tags=not any_tag,
                                          origin=origin, status=status, demo=demo, order=order, fr=bool(fr),
                                          adult=bool(adult), offset=offset, limit=PAGE))
        except Exception as e:
            return render(request, "_discover.html", items=[], error=f"MangaDex injoignable : {type(e).__name__}",
                          more_url=None, total=0, offset=offset)
        mine = followed_ids()
        shelf = library_scan.index_by_norm(library_scan.scan(ctx), autoadd.norm)
        auto_translate = translate.get_config(repo)["provider"] != "none"
        for it in items:
            it["followed"] = it["id"] in mine
            it["in_library"] = any(autoadd.norm(t) in shelf for t in [it["title"]] + it["alt_titles"])
            it["short"] = shorten(it["synopsis"])
            it["needs_tr"] = bool(it["synopsis"]) and it["synopsis_lang"] != "fr" and auto_translate
            known = translate.cached(ctx, it["synopsis"], it["synopsis_lang"]) if it["needs_tr"] else None
            if known:                                  # déjà traduit auparavant : affiché tout de suite
                it["synopsis"], it["needs_tr"], it["short"] = known, False, shorten(known)
            if it["needs_tr"]:
                remember(it)
            it["type_label"] = mangadex.TYPES.get(it["type"], "")
            it["status_label"] = mangadex.STATUSES.get(it["status"], "")
        nxt = offset + PAGE
        more_url = None
        if nxt < min(total, 10000):
            more_url = "/discover/results?" + urlencode(
                [(k, v) for k, v in qp.multi_items() if k != "offset"] + [("offset", nxt)])
        return render(request, "_discover.html", items=items, error=None, more_url=more_url, total=total,
                      offset=offset)

    @app.post("/discover/follow")
    async def discover_follow(request: Request):
        """Suivre en un clic : source trouvée automatiquement, dossier existant repris, modifiable ensuite."""
        form = await request.form()
        md_id = (form.get("id") or "").strip()
        if not UUID.match(md_id):
            return Response("identifiant invalide", status_code=400)
        wants_json = "application/json" in request.headers.get("accept", "")

        def work():
            item = mangadex.one(ctx.fast_session, api(), md_id)
            item["id"] = md_id
            return autoadd.auto_add(ctx, item)

        try:
            res = await run_in_threadpool(work)
        except Exception as e:
            msg = f"MangaDex injoignable : {type(e).__name__}"
            return JSONResponse({"ok": False, "message": msg}, status_code=502) if wants_json \
                else Response(msg, status_code=502)
        level, text = autoadd.describe(res)
        res.update(ok=True, level=level, message=text, url=f"/manga/{res['id']}")
        return JSONResponse(res) if wants_json else RedirectResponse(res["url"], status_code=303)

    @app.get("/discover/summary", response_class=HTMLResponse)
    async def discover_summary(request: Request, id: str = ""):
        """Résumé traduit en français (appelé en différé pour chaque carte non francophone)."""
        if not UUID.match(id):
            return Response("identifiant invalide", status_code=400)

        def work():
            if id not in SYN_CACHE:
                it = mangadex.one(ctx.fast_session, api(), id)
                remember(it, id)
            text, lang = SYN_CACHE[id]
            fr, err = translate.translate(ctx, text, lang)
            return text, fr, err

        try:
            text, fr, err = await run_in_threadpool(work)
        except Exception as e:
            text, fr, err = "", None, f"résumé indisponible ({type(e).__name__})"
        shown = fr or text
        return render(request, "_summary.html", short=shorten(shown), full=shown, translated=bool(fr), error=err)

    # ------------------------------------------------------------ proxy des jaquettes
    def fetch_cover(url):
        """(octets, erreur) : cache disque, sinon téléchargement par le serveur."""
        cache_dir = os.path.join(ctx.settings.cache_dir, "mdcovers")
        path = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest())
        try:
            with open(path, "rb") as f:
                return f.read(), None
        except OSError:
            pass
        try:
            r = ctx.fast_session.get(url, headers={"User-Agent": MD_HEADERS["User-Agent"]}, timeout=(10, 30))
        except Exception as e:
            return None, f"{type(e).__name__}"
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        if len(r.content) > MAX_COVER or sniff_image_ext(r.content) is None:
            return None, "réponse non-image"
        os.makedirs(cache_dir, exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(r.content)
        os.replace(path + ".tmp", path)
        return r.content, None

    @app.get("/proxy/cover")
    async def proxy_cover(u: str = ""):
        parts = urlparse(u)
        if parts.scheme not in ALLOWED_SCHEMES or parts.netloc not in ALLOWED_HOSTS \
                or not COVER_PATH.match(parts.path) or parts.query or parts.fragment:
            return Response("adresse non autorisée", status_code=400)
        data, err = await run_in_threadpool(fetch_cover, u)
        if data is None:
            return Response(f"jaquette indisponible ({err})", status_code=502)
        return Response(data, media_type=MIME.get(sniff_image_ext(data), "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=604800"})
