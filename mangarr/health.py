"""Santé de l'installation, sauvegardes, contrôle de la bibliothèque et estimation des prochaines sorties."""
import io
import os
import re
import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
import zipfile
from zipfile import BadZipFile, ZipFile

import requests

from mangarr.core.net import MD_HEADERS
from mangarr.core.util import permission_hint
from mangarr.tomes.plan import chapter_numbers

GB = 1024 ** 3
KEEP_BACKUPS = 7
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
OK, WARN, BAD = "ok", "warn", "bad"


# --------------------------------------------------------------------------- diagnostic local
def _writable(path):
    """(ok, détail) : le dossier existe (ou se crée) et on peut y écrire."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path, prefix=".mangarr-test-")
        os.close(fd)
        os.remove(tmp)
        return True, "lecture et écriture OK"
    except OSError as e:
        hint = permission_hint(path) if isinstance(e, PermissionError) else ""
        return False, f"{e}. {hint}".strip()


def _disk(path):
    try:
        u = shutil.disk_usage(path if os.path.exists(path) else os.path.dirname(path) or "/")
    except OSError:
        return None
    return u.free, u.total


def checks(ctx):
    """Liste de {label, level, detail} : droits d'écriture, espace disque, base, planificateur."""
    st, out = ctx.settings, []
    for label, path in (("Bibliothèque (tomes)", st.library_dir), ("Chapitres en cours", st.chapters_dir),
                        ("Configuration", st.config_dir), ("Jaquettes", st.covers_dir),
                        ("Exports Patreon", st.patreon_dir)):
        ok, detail = _writable(path)
        out.append({"label": f"{label} : {path}", "level": OK if ok else BAD, "detail": detail})
    for label, path in (("Bibliothèque", st.library_dir), ("Configuration", st.config_dir)):
        d = _disk(path)
        if d:
            free, total = d
            level = BAD if free < 1 * GB else WARN if free < 5 * GB or free < total * 0.05 else OK
            out.append({"label": f"Espace disque — {label}", "level": level,
                        "detail": f"{free / GB:.1f} Go libres sur {total / GB:.1f} Go"})
    try:
        ctx.db.one("SELECT 1")
        out.append({"label": "Base de données", "level": OK, "detail": st.db_path})
    except Exception as e:                                  # noqa: BLE001
        out.append({"label": "Base de données", "level": BAD, "detail": str(e)})
    minutes = ctx.repo.get_setting("check_interval_min", st.check_interval_min)
    out.append({"label": "Vérification automatique", "level": OK if minutes else WARN,
                "detail": f"toutes les {minutes} minutes" if minutes else "désactivée (réglage à 0)"})
    return out


def sources_overview(ctx):
    """Par source : nombre de mangas, en erreur, à configurer, dernier message d'erreur."""
    from mangarr.autoadd import needs_setup
    rows = {}
    for m in ctx.repo.list_mangas():
        s = ctx.repo.get_state(m["id"])
        r = rows.setdefault(m["source"], {"source": m["source"], "total": 0, "error": 0, "setup": 0, "msg": ""})
        r["total"] += 1
        if needs_setup(m, s):
            r["setup"] += 1
        elif s.get("last_status") == "error":
            r["error"] += 1
            r["msg"] = r["msg"] or f"{m['name']} : {(s.get('last_message') or '')[:120]}"
    return sorted(rows.values(), key=lambda r: (-r["error"], r["source"]))


# --------------------------------------------------------------------------- services distants
def _ping(session, url, **kw):
    t0 = time.time()
    try:
        r = session.get(url, timeout=(5, 8), headers=MD_HEADERS, **kw)
    except requests.RequestException as e:
        return BAD, f"injoignable ({type(e).__name__})", None
    ms = int((time.time() - t0) * 1000)
    if r.status_code >= 500 or r.status_code in (403, 429):
        return WARN, f"HTTP {r.status_code}", ms
    return OK, f"répond en {ms} ms", ms


def services(ctx):
    """État des APIs externes utilisées (appel réseau : à charger à la demande)."""
    st, s = ctx.settings, ctx.fast_session
    out = []
    lvl, d, _ = _ping(s, f"{st.mangadex_api}/ping")
    out.append({"label": "MangaDex (tomes, jaquettes)", "level": lvl, "detail": d})
    lvl, d, _ = _ping(s, f"{st.jikan_api}/manga/1")
    out.append({"label": "MyAnimeList via Jikan (actus, auteurs)", "level": lvl, "detail": d})
    if st.reddit_client_id and st.reddit_client_secret:
        from mangarr import news
        try:
            news.reddit_token(s, st)
            out.append({"label": "Reddit (API officielle)", "level": OK, "detail": "jeton obtenu"})
        except Exception as e:                              # noqa: BLE001
            out.append({"label": "Reddit (API officielle)", "level": BAD, "detail": str(e)})
    else:
        out.append({"label": "Reddit (API officielle)", "level": WARN,
                    "detail": "non configuré (MANGARR_REDDIT_CLIENT_ID / SECRET)"})
    seen = (ctx.repo.get_setting("patreon_sync", {}) or {}).get("seen")
    has_patreon = any(m["source"] == "patreon" for m in ctx.repo.list_mangas())
    if has_patreon:
        ago = (time.time() - seen) / 3600 if seen else None
        out.append({"label": "Script Patreon (navigateur)", "level": OK if ago is not None and ago < 8 else WARN,
                    "detail": "jamais vu" if ago is None else f"vu il y a {ago:.1f} h"})
    return out


# --------------------------------------------------------------------------- sauvegardes
def backup_dir(ctx):
    return os.path.join(ctx.settings.config_dir, "backups")


def backup_now(ctx):
    """Copie cohérente de la base SQLite (API backup, sûre même en cours d'écriture). Garde les 7 dernières."""
    os.makedirs(backup_dir(ctx), exist_ok=True)
    dest = os.path.join(backup_dir(ctx), time.strftime("mangarr-%Y%m%d-%H%M%S.db"))
    src, dst = sqlite3.connect(ctx.settings.db_path), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in list_backups(ctx)[KEEP_BACKUPS:]:
        try:
            os.remove(old["path"])
        except OSError:
            pass
    return dest


def list_backups(ctx):
    try:
        names = sorted((f for f in os.listdir(backup_dir(ctx)) if re.fullmatch(r"mangarr-\d{8}-\d{6}\.db", f)),
                       reverse=True)
    except OSError:
        return []
    return [{"name": n, "path": os.path.join(backup_dir(ctx), n),
             "size": os.path.getsize(os.path.join(backup_dir(ctx), n)),
             "ts": os.path.getmtime(os.path.join(backup_dir(ctx), n))} for n in names]


def export_zip(ctx):
    """Archive à télécharger : copie de la base + exports Patreon (les jaquettes se retrouvent seules)."""
    snap = backup_now(ctx)
    buf = io.BytesIO()
    with ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "mangarr.db")
        pdir = ctx.settings.patreon_dir
        if os.path.isdir(pdir):
            for f in os.listdir(pdir):
                if f.endswith(".json"):
                    z.write(os.path.join(pdir, f), f"patreon/{f}")
    return buf.getvalue()


# --------------------------------------------------------------------------- contrôle de la bibliothèque
STATE = {"running": False, "done": 0, "total": 0, "result": None, "ts": None}
LOCK = threading.Lock()


def _manga_dirs(ctx, m):
    tomes = m.get("tomes_folder") or os.path.join(ctx.settings.library_dir, m["name"])
    chapters = m.get("folder") or os.path.join(ctx.settings.chapters_dir, m["name"])
    return chapters, tomes


def _check_cbz(path):
    """None si le CBZ est sain, sinon la raison."""
    try:
        with ZipFile(path) as z:
            if not any(n.lower().endswith(IMG_EXT) for n in z.namelist()):
                return "aucune image dedans"
            bad = z.testzip()
            if bad:
                return f"fichier corrompu ({bad})"
    except (BadZipFile, OSError, ValueError) as e:
        return f"illisible ({type(e).__name__})"
    return None


def _gaps(numbers):
    if len(numbers) < 2:
        return []
    return sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))


def scan_library(ctx):
    """{mangas: [{name, corrupt, gaps_ch, gaps_tomes, parts}], totals}. Peut durer : à lancer en arrière-plan."""
    mangas = ctx.repo.list_mangas()
    STATE.update(total=len(mangas), done=0)
    out = []
    for m in mangas:
        name, found = m["name"], {"name": m["name"], "id": m["id"], "corrupt": [], "gaps_ch": [], "gaps_tomes": [],
                                  "parts": []}
        chapters, tomes = _manga_dirs(ctx, m)
        for d in {chapters, tomes}:
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.endswith(".part"):
                    found["parts"].append(p)
                elif f.lower().endswith(".cbz"):
                    why = _check_cbz(p)
                    if why:
                        found["corrupt"].append({"file": f, "why": why})
        found["gaps_ch"] = _gaps(chapter_numbers(chapters, name)) if m["source"] != "patreon" else []
        if os.path.isdir(tomes):
            tn = {int(g.group(1)) for f in os.listdir(tomes) if (g := re.fullmatch(rf"{re.escape(name)}_T(\d+)\.cbz", f))}
            found["gaps_tomes"] = _gaps(tn)
        if found["corrupt"] or found["gaps_ch"] or found["gaps_tomes"] or found["parts"]:
            out.append(found)
        STATE["done"] += 1
    return {"mangas": out, "checked": len(mangas),
            "totals": {"corrupt": sum(len(x["corrupt"]) for x in out), "gaps": sum(len(x["gaps_ch"]) + len(x["gaps_tomes"]) for x in out),
                       "parts": sum(len(x["parts"]) for x in out)}}


def scan_async(ctx):
    if LOCK.locked():
        return False

    def work():
        with LOCK:
            STATE.update(running=True, result=None)
            try:
                STATE["result"] = scan_library(ctx)
            except Exception as e:                          # noqa: BLE001
                STATE["result"] = {"error": f"{type(e).__name__}: {e}", "mangas": [], "checked": 0,
                                   "totals": {"corrupt": 0, "gaps": 0, "parts": 0}}
            STATE.update(running=False, ts=time.time())

    STATE.update(running=True, result=None, done=0)
    threading.Thread(target=work, daemon=True, name="library-scan").start()
    return True


def clean_parts(ctx):
    """Supprime les fichiers .part (téléchargements interrompus) trouvés par le dernier contrôle. Retourne le nombre."""
    roots = [os.path.realpath(ctx.settings.library_dir), os.path.realpath(ctx.settings.chapters_dir)]
    n = 0
    for m in (STATE.get("result") or {}).get("mangas", []):
        for p in m["parts"]:
            rp = os.path.realpath(p)
            if p.endswith(".part") and any(rp.startswith(r + os.sep) for r in roots):
                try:
                    os.remove(rp)
                    n += 1
                except OSError:
                    pass
    if STATE.get("result"):
        for m in STATE["result"]["mangas"]:
            m["parts"] = [p for p in m["parts"] if os.path.exists(p)]
        STATE["result"]["totals"]["parts"] = sum(len(m["parts"]) for m in STATE["result"]["mangas"])
    return n


# --------------------------------------------------------------------------- calendrier
def forecast(ctx, now=None):
    """Par manga : dernier téléchargement, rythme habituel (médiane des écarts) et prochaine sortie estimée."""
    now = now or time.time()
    rows = []
    for m in ctx.repo.list_mangas():
        kind = "tome" if m["source"] == "patreon" else "chapter"
        ts = sorted(h["ts"] for h in ctx.repo.history(m["id"], 40) if h["kind"] == kind)
        if not ts:
            continue
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 3600]      # ignore les rafales (rattrapage d'un coup)
        row = {"name": m["name"], "id": m["id"], "last": ts[-1], "pace": None, "next": None, "late": False,
               "enabled": bool(m["enabled"])}
        if len(gaps) >= 2:
            pace = statistics.median(gaps[-6:])
            row.update(pace=pace, next=ts[-1] + pace, late=ts[-1] + pace < now)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["next"] is None, r["next"] or 0))


def activity(ctx, days=14, now=None):
    """Téléchargements récents groupés par jour : [(jour, [{name, kind, number}])]."""
    now = now or time.time()
    names = {m["id"]: m["name"] for m in ctx.repo.list_mangas()}
    by_day = {}
    for h in ctx.repo.history(None, 400):
        if h["ts"] < now - days * 86400:
            continue
        day = time.strftime("%d/%m/%Y", time.localtime(h["ts"]))
        by_day.setdefault(day, []).append({"name": names.get(h["manga_id"], "?"), "kind": h["kind"],
                                           "number": h["number"]})
    return list(by_day.items())
