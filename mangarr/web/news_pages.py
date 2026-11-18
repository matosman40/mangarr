"""Pages « Actus » : news, fiche, auteurs et discussions (spoilers possibles) de chaque manga suivi."""
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from mangarr import news

SAFE_PREFIX = ("https://myanimelist.net/", "https://www.reddit.com/")      # liens et images venant de l'API : on n'affiche rien d'autre
SAFE_IMG = ("https://cdn.myanimelist.net/",)


def _clean(data):
    """Retire toute URL qui ne pointe pas vers MyAnimeList ou Reddit (aucun lien javascript:/externe inattendu)."""
    if not data:
        return data
    ok = lambda u: u if isinstance(u, str) and u.startswith(SAFE_PREFIX) else None
    img = lambda u: u if isinstance(u, str) and u.startswith(SAFE_IMG) else None
    data["url"] = ok(data.get("url"))
    data["feed"] = [{**n, "url": ok(n.get("url"))} for n in data.get("feed", [])]
    for a in data.get("authors", []):
        a["url"], a["image"] = ok(a.get("url")), img(a.get("image"))
    return data


def register(app, ctx, render):
    repo = ctx.repo

    @app.get("/news", response_class=HTMLResponse)
    def news_page(request: Request):
        return render(request, "news.html", mangas=repo.list_mangas())

    @app.get("/manga/{mid}/news", response_class=HTMLResponse)
    async def manga_news(request: Request, mid: int, refresh: int = 0):
        m = repo.get_manga(mid)
        if not m:
            return Response("Manga introuvable", status_code=404)
        data, err = await run_in_threadpool(news.get, ctx, ctx.fast_session, m, bool(refresh))
        return render(request, "_news.html", m=m, d=_clean(data), error=err)
