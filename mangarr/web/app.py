"""Interface web : FastAPI + Jinja2 + HTMX. Authentification optionnelle (MANGARR_PASSWORD)."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mangarr import __version__
from mangarr.core.engine import RUN_LOCK, RunOptions, check_all
from mangarr.tomes.plan import chapter_numbers

BASE = Path(__file__).parent
COOKIE = "mangarr_session"
SESSION_TTL = 7 * 24 * 3600
PUBLIC = ("/health", "/login", "/static")


class Auth:
    """Mot de passe unique ; cookie signé (HMAC), HttpOnly, SameSite=Lax. Désactivé si pas de mot de passe."""

    def __init__(self, password, config_dir):
        self.password = password or ""
        key_file = os.path.join(config_dir, "secret.key")
        os.makedirs(config_dir, exist_ok=True)
        if not os.path.isfile(key_file):
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        with open(key_file) as f:
            self.key = f.read().strip().encode()

    @property
    def enabled(self):
        return bool(self.password)

    def check_password(self, given):
        return hmac.compare_digest(given.encode(), self.password.encode())

    def _sig(self, payload):
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def make_token(self):
        payload = str(int(time.time()) + SESSION_TTL)
        return f"{payload}.{self._sig(payload)}"

    def valid(self, token):
        try:
            payload, sig = token.split(".")
            return hmac.compare_digest(sig, self._sig(payload)) and int(payload) > time.time()
        except (ValueError, AttributeError):
            return False


def fmt_ts(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "jamais"


def manga_view(ctx, m):
    """Données d'affichage d'un manga : état, nombre de tomes, prochain tome."""
    st = ctx.repo.get_state(m["id"])
    lib = m.get("tomes_folder") or os.path.join(ctx.settings.library_dir, m["name"])
    tomes = sorted(f for f in os.listdir(lib) if f.endswith(".cbz")) if os.path.isdir(lib) else []
    return {**m, "state": st, "last_check_txt": fmt_ts(st.get("last_check")), "tomes_count": len(tomes),
            "next_tome": next_tome(ctx, m), "has_cover": bool(m.get("cover") and os.path.isfile(m["cover"]))}


def next_tome(ctx, m):
    """« T3 : 5/8 chapitres » d'après le cache MangaDex ; None si inconnu."""
    import re
    cache = os.path.join(ctx.settings.cache_dir, f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(m['id']))}.volumes.json")
    try:
        with open(cache) as f:
            vols = {int(k): v for k, v in json.load(f)["volumes"].items()}
    except (OSError, ValueError, KeyError):
        return None
    folder = m.get("folder") or os.path.join(ctx.settings.chapters_dir, m["name"])
    local = chapter_numbers(folder, m["name"])
    for vol in sorted(vols):
        chs = vols[vol]
        if chs and (set(chs) & local) and set(chs) - local:
            return f"T{vol} : {len(set(chs) & local)}/{len(chs)} chapitres"
    last = max(local) if local else None
    for vol in sorted(vols):
        if vols[vol] and last is not None and vols[vol][0] > last:
            return f"T{vol} à venir"
    return None


def create_app(ctx, scheduler=None, password=None):
    app = FastAPI(title="Mangarr", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.globals.update(version=__version__, fmt_ts=fmt_ts)
    auth = Auth(password if password is not None else os.environ.get("MANGARR_PASSWORD", ""),
                ctx.settings.config_dir)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    app.state.ctx, app.state.auth = ctx, auth

    def render(request, name, **kw):
        return templates.TemplateResponse(request, name, {"auth_enabled": auth.enabled,
                                                          "busy": RUN_LOCK.locked(), **kw})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if auth.enabled and not path.startswith(PUBLIC) and not auth.valid(request.cookies.get(COOKIE, "")):
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.get("/health")
    def health():
        ctx.db.one("SELECT 1")
        return {"status": "ok", "version": __version__}

    # ---- authentification ----
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        if not auth.enabled or auth.check_password(password):
            resp = RedirectResponse("/", status_code=303)
            if auth.enabled:
                resp.set_cookie(COOKIE, auth.make_token(), httponly=True, samesite="lax",
                                max_age=SESSION_TTL, secure=request.url.scheme == "https")
            return resp
        time.sleep(1)   # freine le brute-force
        return render(request, "login.html", error="Mot de passe incorrect")

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ---- pages ----
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        mangas = [manga_view(ctx, m) for m in ctx.repo.list_mangas()]
        return render(request, "index.html", mangas=mangas)

    @app.get("/grid", response_class=HTMLResponse)
    def grid(request: Request):
        mangas = [manga_view(ctx, m) for m in ctx.repo.list_mangas()]
        return render(request, "_grid.html", mangas=mangas)

    @app.get("/manga/{mid}", response_class=HTMLResponse)
    def detail(request: Request, mid: int):
        m = ctx.repo.get_manga(mid)
        if not m:
            return Response("Manga introuvable", status_code=404)
        return render(request, "manga.html", m=manga_view(ctx, m), history=ctx.repo.history(mid, 50),
                      logs=ctx.repo.logs(60, mid), params=json.dumps(m["params"], indent=2, ensure_ascii=False))

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request):
        names = {m["id"]: m["name"] for m in ctx.repo.list_mangas()}
        interval = ctx.repo.get_setting("check_interval_min", ctx.settings.check_interval_min)
        return render(request, "logs.html", logs=ctx.repo.logs(300), names=names, interval=interval,
                      history=ctx.repo.history(limit=50))

    @app.get("/logs/rows", response_class=HTMLResponse)
    def log_rows(request: Request):
        names = {m["id"]: m["name"] for m in ctx.repo.list_mangas()}
        return render(request, "_logrows.html", logs=ctx.repo.logs(300), names=names)

    @app.get("/cover/{mid}")
    def cover(mid: int):
        m = ctx.repo.get_manga(mid)
        if not m or not m.get("cover") or not os.path.isfile(m["cover"]):
            return Response(status_code=404)
        return FileResponse(m["cover"], headers={"Cache-Control": "public, max-age=3600"})

    # ---- actions ----
    def launch(only=None):
        if RUN_LOCK.locked():
            return False
        if scheduler:
            scheduler.run_now_async(only)
        else:   # sans planificateur (tests) : exécution directe
            check_all(ctx, only=only, opts=RunOptions())
        return True

    @app.post("/check")
    def check_everything(request: Request):
        launch()
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/manga/{mid}/check")
    def check_one(request: Request, mid: int):
        m = ctx.repo.get_manga(mid)
        if m:
            launch([m["name"]])
        return RedirectResponse(f"/manga/{mid}", status_code=303)

    @app.post("/manga/{mid}/toggle")
    def toggle(mid: int):
        m = ctx.repo.get_manga(mid)
        if m:
            ctx.repo.update_manga(mid, enabled=not m["enabled"])
        return RedirectResponse(f"/manga/{mid}", status_code=303)

    @app.post("/manga/{mid}/delete")
    def delete(mid: int):
        """Retire du suivi ; les fichiers de la bibliothèque ne sont jamais supprimés."""
        ctx.repo.delete_manga(mid)
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/interval")
    def set_interval(minutes: int = Form(...)):
        minutes = max(0, min(minutes, 60 * 24 * 30))
        if scheduler:
            scheduler.set_interval(minutes)
        else:
            ctx.repo.set_setting("check_interval_min", minutes)
        return RedirectResponse("/logs", status_code=303)

    return app
