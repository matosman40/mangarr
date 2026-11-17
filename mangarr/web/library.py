"""Page « Bibliothèque » : mangas déjà présents dans les dossiers, à suivre en un clic."""
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from mangarr import autoadd, library_scan

BULK_LIMIT = 25      # une série = une recherche MangaDex + une recherche par site : on limite chaque passage


def register(app, ctx, render):
    def api():
        return ctx.settings.mangadex_api

    @app.get("/library", response_class=HTMLResponse)
    async def library_page(request: Request, done: int = 0, left: int = 0):
        series = await run_in_threadpool(library_scan.scan, ctx, True)
        return render(request, "library.html", series=series, done=done, left=left,
                      untracked=sum(1 for s in series if not s["tracked"]),
                      library_dir=ctx.settings.library_dir, chapters_dir=ctx.settings.chapters_dir)

    @app.post("/library/adopt")
    async def adopt(name: str = Form("")):
        series = next((s for s in library_scan.scan(ctx, fresh=True) if s["name"] == name and not s["tracked"]), None)
        if not series:
            return Response("série introuvable ou déjà suivie", status_code=404)
        res = await run_in_threadpool(autoadd.adopt_folder, ctx, series, api())
        return RedirectResponse(f"/manga/{res['id']}", status_code=303)

    @app.post("/library/adopt-all")
    async def adopt_all():
        todo = [s for s in await run_in_threadpool(library_scan.scan, ctx, True) if not s["tracked"]]
        batch, rest = todo[:BULK_LIMIT], max(0, len(todo) - BULK_LIMIT)

        def work():
            for s in batch:
                try:
                    autoadd.adopt_folder(ctx, s, api())
                except Exception as e:      # une série en échec n'arrête pas les autres
                    ctx.repo.add_log("warn", f"Import du dossier « {s['name']} » : {type(e).__name__}")
            return len(batch)

        n = await run_in_threadpool(work)
        return RedirectResponse(f"/library?done={n}&left={rest}", status_code=303)
