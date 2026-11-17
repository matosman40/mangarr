"""Page « Découvrir » : catalogue MangaDex filtrable (genres, type, statut, tri), jaquettes et synopsis."""
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from mangarr.tomes import mangadex

PAGE = 24
SYNOPSIS_SHORT = 240


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
    async def discover_results(request: Request, q: str = "", origin: str = "", order: str = "latestUploadedChapter",
                               offset: int = 0, fr: str = "", adult: str = "", any_tag: str = ""):
        qp = request.query_params
        tags, status, demo = qp.getlist("tag"), qp.getlist("status"), qp.getlist("demo")
        offset = max(0, min(offset, 9900))
        try:
            items, total = await run_in_threadpool(
                lambda: mangadex.discover(ctx.fast_session, api(), q=q.strip(), tags=tags, all_tags=not any_tag,
                                          origin=origin, status=status, demo=demo, order=order, fr=bool(fr),
                                          adult=bool(adult), offset=offset, limit=PAGE))
        except Exception as e:
            return render(request, "_discover.html", items=[], error=f"MangaDex injoignable : {type(e).__name__}",
                          more_url=None, total=0, offset=offset)
        mine = followed_ids()
        for it in items:
            it["followed"] = it["id"] in mine
            syn = it["synopsis"]
            it["short"] = (syn[:SYNOPSIS_SHORT].rsplit(" ", 1)[0] + "…") if len(syn) > SYNOPSIS_SHORT else syn
            it["type_label"] = mangadex.TYPES.get(it["type"], "")
            it["status_label"] = mangadex.STATUSES.get(it["status"], "")
        nxt = offset + PAGE
        more_url = None
        if nxt < min(total, 10000):
            more_url = "/discover/results?" + urlencode(
                [(k, v) for k, v in qp.multi_items() if k != "offset"] + [("offset", nxt)])
        return render(request, "_discover.html", items=items, error=None, more_url=more_url, total=total,
                      offset=offset)
