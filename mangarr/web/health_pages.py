"""Pages Santé (diagnostic, sauvegardes), Qualité de la bibliothèque et Calendrier des sorties."""
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from mangarr import health


def register(app, ctx, render):

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request, msg: str = ""):
        items = health.checks(ctx)
        order = {"bad": 0, "warn": 1, "ok": 2}
        worst = min((i["level"] for i in items), key=order.get, default="ok")
        return render(request, "status.html", items=items, worst=worst, sources=health.sources_overview(ctx),
                      backups=health.list_backups(ctx), msg=msg, now=time.time(),
                      has_mangas=bool(ctx.repo.list_mangas()))

    @app.get("/status/services", response_class=HTMLResponse)
    async def status_services(request: Request):
        items = await run_in_threadpool(health.services, ctx)
        return render(request, "_services.html", items=items)

    @app.post("/backup/now")
    async def backup_now():
        try:
            path = await run_in_threadpool(health.backup_now, ctx)
            msg = "Sauvegarde créée : " + path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        except Exception as e:                              # noqa: BLE001
            msg = f"Sauvegarde impossible : {type(e).__name__}: {e}"
        return RedirectResponse("/status?msg=" + _q(msg), status_code=303)

    @app.get("/backup/download")
    async def backup_download():
        data = await run_in_threadpool(health.export_zip, ctx)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="mangarr-export-{time.strftime("%Y%m%d")}.zip"'})

    # ---------------------------------------------------------------- qualité
    @app.get("/quality", response_class=HTMLResponse)
    def quality(request: Request):
        return render(request, "quality.html", state=health.STATE, res=health.STATE["result"], now=time.time())

    @app.post("/quality/scan")
    def quality_scan():
        health.scan_async(ctx)
        return RedirectResponse("/quality", status_code=303)

    @app.get("/quality/result", response_class=HTMLResponse)
    def quality_result(request: Request):
        return render(request, "_quality.html", state=health.STATE, res=health.STATE["result"])

    @app.post("/quality/clean")
    def quality_clean():
        health.clean_parts(ctx)
        return RedirectResponse("/quality", status_code=303)

    # ---------------------------------------------------------------- calendrier
    @app.get("/calendar", response_class=HTMLResponse)
    def calendar(request: Request):
        return render(request, "calendar.html", rows=health.forecast(ctx), days=health.activity(ctx),
                      now=time.time())


def _q(s):
    from urllib.parse import quote
    return quote(s)
