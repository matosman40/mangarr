"""Pages Réglages (notifications, bibliothèques) et Import de l'ancienne configuration."""
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from mangarr import notify, translate
from mangarr.importer import import_mangas, parse_config

MAX_IMPORT = 2 * 1024 * 1024

# service -> (champs du formulaire -> clé stockée)
NOTIF_FIELDS = {
    "discord": {"discord_url": "url"},
    "telegram": {"telegram_token": "token", "telegram_chat_id": "chat_id"},
    "ntfy": {"ntfy_url": "url", "ntfy_token": "token"},
}
LIB_FIELDS = {
    "komga": {"komga_url": "url", "komga_library": "library_id", "komga_user": "user",
              "komga_password": "password", "komga_api_key": "api_key"},
    "kavita": {"kavita_url": "url", "kavita_library": "library_id", "kavita_api_key": "api_key"},
}


def merge(old, fields, form, secrets):
    """Nouvelle config d'un service. Un secret laissé vide est conservé ; « supprimer » efface tout."""
    new = {}
    for field, key in fields.items():
        val = (form.get(field) or "").strip()
        if not val and key in secrets:
            val = old.get(key, "")
        if val:
            new[key] = val
    return new


def view(cfg, fields, secrets):
    """Valeurs à afficher : les secrets ne sont jamais renvoyés au navigateur."""
    out = {}
    for field, key in fields.items():
        out[field] = "" if key in secrets else cfg.get(key, "")
        out[field + "_set"] = bool(cfg.get(key)) and key in secrets
    return out


def register(app, ctx, render):
    repo = ctx.repo

    def page_ctx():
        notif = repo.get_setting("notifications", {})
        integ = repo.get_setting("integrations", {})
        values = {}
        for svc, fields in NOTIF_FIELDS.items():
            values.update(view(notif.get(svc, {}), fields, notify.SECRETS[svc]))
        for svc, fields in LIB_FIELDS.items():
            values.update(view(integ.get(svc, {}), fields, notify.SECRETS[svc]))
        tr = translate.get_config(repo)
        values["only_sources"] = bool(repo.get_setting("discover_only_sources", True))
        values["auto_start"] = repo.get_setting("auto_add", {}).get("start", "first")
        values.update(tr_provider=tr["provider"], tr_url=tr.get("url", ""), tr_key_set=bool(tr.get("api_key")))
        return dict(values=values, providers=translate.PROVIDERS, notif=notif, integ=integ,
                    on_new=notif.get("on_new", True), on_error=notif.get("on_error", True))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: str = ""):
        return render(request, "settings.html", saved=saved, **page_ctx())

    @app.post("/settings/notifications")
    async def save_notifications(request: Request):
        form = await request.form()
        cfg = repo.get_setting("notifications", {})
        for svc, fields in NOTIF_FIELDS.items():
            if form.get(f"clear_{svc}"):
                cfg.pop(svc, None)
                continue
            new = merge(cfg.get(svc, {}), fields, form, notify.SECRETS[svc])
            if new:
                cfg[svc] = new
            else:
                cfg.pop(svc, None)
        cfg["on_new"] = form.get("on_new") is not None
        cfg["on_error"] = form.get("on_error") is not None
        repo.set_setting("notifications", cfg)
        return RedirectResponse("/settings?saved=notifications", status_code=303)

    @app.post("/settings/library")
    async def save_library(request: Request):
        form = await request.form()
        cfg = repo.get_setting("integrations", {})
        for svc, fields in LIB_FIELDS.items():
            if form.get(f"clear_{svc}"):
                cfg.pop(svc, None)
                continue
            new = merge(cfg.get(svc, {}), fields, form, notify.SECRETS[svc])
            if new:
                cfg[svc] = new
            else:
                cfg.pop(svc, None)
        repo.set_setting("integrations", cfg)
        return RedirectResponse("/settings?saved=library", status_code=303)

    @app.post("/settings/discover")
    async def save_discover(request: Request):
        form = await request.form()
        repo.set_setting("discover_only_sources", form.get("only_sources") is not None)
        return RedirectResponse("/settings?saved=discover", status_code=303)

    @app.post("/settings/autoadd")
    async def save_autoadd(request: Request):
        form = await request.form()
        start = form.get("start", "first")
        repo.set_setting("auto_add", {"start": start if start in ("first", "latest") else "first"})
        return RedirectResponse("/settings?saved=autoadd", status_code=303)

    @app.post("/settings/translation")
    async def save_translation(request: Request):
        form = await request.form()
        old = translate.get_config(repo)
        provider = form.get("provider", "mymemory")
        url = (form.get("tr_url") or "").strip()
        if provider not in translate.PROVIDERS or (url and not url.startswith(("http://", "https://"))):
            return RedirectResponse("/settings?saved=translation-invalide", status_code=303)
        cfg = {"provider": provider, "url": url, "fallback": provider in ("google", "mymemory")}
        key = (form.get("tr_key") or "").strip() or ("" if form.get("clear_tr_key") else old.get("api_key", ""))
        if key:
            cfg["api_key"] = key
        repo.set_setting("translation", cfg)
        return RedirectResponse("/settings?saved=translation", status_code=303)

    @app.post("/settings/translation/test", response_class=HTMLResponse)
    async def test_translation(request: Request):
        text, err = await run_in_threadpool(translate.translate, ctx, "The strongest hero returns to save the world.", "en")
        return render(request, "_svc_results.html", results=[("traduction", err is None, text if not err else err)],
                      empty="")

    @app.post("/settings/notifications/test", response_class=HTMLResponse)
    async def test_notifications(request: Request):
        res = await run_in_threadpool(notify.send_all, ctx, "Mangarr", "Notification de test ✅")
        return render(request, "_svc_results.html", results=res, empty="Aucun service de notification enregistré.")

    @app.post("/settings/library/test", response_class=HTMLResponse)
    async def test_library(request: Request):
        res = await run_in_threadpool(notify.refresh_libraries, ctx)
        return render(request, "_svc_results.html", results=res, empty="Aucune bibliothèque enregistrée.")

    # ------------------------------------------------------------------ import
    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html", rows=None, error=None, applied=False)

    @app.post("/import", response_class=HTMLResponse)
    async def import_submit(request: Request):
        form = await request.form()
        up = form.get("file")
        applied = form.get("apply") is not None
        try:
            if up is None or not hasattr(up, "read"):
                raise ValueError("choisissez le fichier manga_dl.py")
            raw = await up.read(MAX_IMPORT + 1)
            if len(raw) > MAX_IMPORT:
                raise ValueError("fichier trop gros (2 Mo max)")
            mangas = parse_config(raw.decode("utf-8-sig"))      # analyse statique : le code n'est jamais exécuté
            rows = import_mangas(ctx, mangas, apply=applied, keep_folders=form.get("keep_folders") is not None)
        except (ValueError, UnicodeDecodeError) as e:
            return render(request, "import.html", status_code=400, rows=None, error=str(e), applied=False)
        return render(request, "import.html", rows=rows, error=None, applied=applied)
