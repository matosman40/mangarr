"""Notifications (Discord, Telegram, ntfy) et rafraîchissement des bibliothèques (Komga, Kavita)."""
import time

import requests

TIMEOUT = 10
SECRETS = {"discord": ["url"], "telegram": ["token"], "ntfy": ["token"],
           "komga": ["password", "api_key"], "kavita": ["api_key"]}


def _http_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_one(kind, cfg, title, message, session=None):
    """Envoie une notification. Retourne (ok, détail)."""
    s = session or requests
    try:
        if kind == "discord":
            if not _http_url(cfg.get("url")):
                return False, "adresse du webhook invalide"
            r = s.post(cfg["url"], json={"content": f"**{title}**\n{message}"[:1900]}, timeout=TIMEOUT)
        elif kind == "telegram":
            if not cfg.get("token") or not cfg.get("chat_id"):
                return False, "jeton ou identifiant de conversation manquant"
            base = cfg.get("api_base") or "https://api.telegram.org"
            r = s.post(f"{base}/bot{cfg['token']}/sendMessage",
                       json={"chat_id": cfg["chat_id"], "text": f"{title}\n{message}"[:4000]}, timeout=TIMEOUT)
        elif kind == "ntfy":
            if not _http_url(cfg.get("url")):
                return False, "adresse ntfy invalide (ex. https://ntfy.sh/mon-sujet)"
            headers = {"Title": title.encode("utf-8")}
            if cfg.get("token"):
                headers["Authorization"] = f"Bearer {cfg['token']}"
            r = s.post(cfg["url"], data=message.encode("utf-8"), headers=headers, timeout=TIMEOUT)
        else:
            return False, f"service inconnu : {kind}"
    except requests.RequestException as e:
        # le message peut contenir l'adresse (jeton) : on n'affiche que le type d'erreur
        return False, f"échec réseau ({type(e).__name__})"
    if r.status_code >= 300:
        return False, f"HTTP {r.status_code}"
    return True, "envoyé"


def send_all(ctx, title, message):
    cfg = ctx.repo.get_setting("notifications", {})
    out = []
    for kind in ("discord", "telegram", "ntfy"):
        if cfg.get(kind):
            ok, info = send_one(kind, cfg[kind], title, message)
            out.append((kind, ok, info))
            if not ok:
                ctx.repo.add_log("warn", f"Notification {kind} : {info}")
    return out


def new_items(ctx, since):
    return ctx.db.query("SELECT h.kind, h.number, h.pages, m.name FROM history h JOIN mangas m "
                        "ON m.id=h.manga_id WHERE h.ts>=? ORDER BY h.id", (since,))


def summarize(rows):
    by = {}
    for r in rows:
        d = by.setdefault(r["name"].replace("_", " "), {"chapter": [], "tome": []})
        d[r["kind"]].append(r["number"])
    lines = []
    for name, d in sorted(by.items()):
        parts = []
        if d["chapter"]:
            parts.append("chapitre(s) " + ", ".join(map(str, d["chapter"])))
        if d["tome"]:
            parts.append("tome(s) " + ", ".join(map(str, d["tome"])))
        lines.append(f"• {name} : " + " ; ".join(parts))
    return "\n".join(lines)


def after_run(ctx, started, results):
    """Après une vérification : notifications (nouveautés, erreurs nouvelles) puis rafraîchissement des bibliothèques."""
    cfg = ctx.repo.get_setting("notifications", {})
    rows = new_items(ctx, started)
    if rows:
        if cfg.get("on_new", True):
            send_all(ctx, "Mangarr : nouveautés", summarize(rows))
        refresh_libraries(ctx)
    # erreurs : on ne notifie que les erreurs nouvelles ou changées (pas toutes les 6 h)
    seen = ctx.repo.get_setting("notified_errors", {})
    current = {r.name: r.message[:200] for r in results if r.status == "error"}
    fresh = {n: m for n, m in current.items() if seen.get(n) != m}
    if fresh and cfg.get("on_error", True):
        send_all(ctx, "Mangarr : erreurs", "\n".join(f"• {n.replace('_', ' ')} : {m}" for n, m in fresh.items()))
    if current != seen:
        ctx.repo.set_setting("notified_errors", current)


# --------------------------------------------------------------------------- #
# Bibliothèques
# --------------------------------------------------------------------------- #
def refresh_one(kind, cfg, session=None):
    s = session or requests
    base = (cfg.get("url") or "").rstrip("/")
    if not _http_url(base):
        return False, "adresse du serveur invalide"
    try:
        if kind == "komga":
            if not cfg.get("library_id"):
                return False, "identifiant de bibliothèque manquant"
            headers, auth = {}, None
            if cfg.get("api_key"):
                headers["X-API-Key"] = cfg["api_key"]
            elif cfg.get("user"):
                auth = (cfg["user"], cfg.get("password", ""))
            r = s.post(f"{base}/api/v1/libraries/{cfg['library_id']}/scan", headers=headers, auth=auth,
                       timeout=TIMEOUT)
        elif kind == "kavita":
            if not cfg.get("api_key") or not cfg.get("library_id"):
                return False, "clé d'API ou identifiant de bibliothèque manquant"
            a = s.post(f"{base}/api/Plugin/authenticate", params={"apiKey": cfg["api_key"],
                                                                   "pluginName": "Mangarr"}, timeout=TIMEOUT)
            if a.status_code >= 300:
                return False, f"authentification refusée (HTTP {a.status_code})"
            token = a.json().get("token")
            r = s.post(f"{base}/api/Library/scan", params={"libraryId": cfg["library_id"]},
                       headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        else:
            return False, f"service inconnu : {kind}"
    except (requests.RequestException, ValueError) as e:
        return False, f"échec ({type(e).__name__})"
    return (r.status_code < 300), ("analyse demandée" if r.status_code < 300 else f"HTTP {r.status_code}")


def refresh_libraries(ctx):
    cfg = ctx.repo.get_setting("integrations", {})
    out = []
    for kind in ("komga", "kavita"):
        if cfg.get(kind):
            ok, info = refresh_one(kind, cfg[kind])
            out.append((kind, ok, info))
            ctx.repo.add_log("info" if ok else "warn", f"{kind} : {info}")
    return out
