"""Synchronisation Patreon pilotée par le navigateur de l'utilisateur.

Patreon bloque les requêtes serveur : Mangarr ne contourne rien. Un userscript (Tampermonkey/Violentmonkey)
tourne dans VOTRE navigateur, déjà connecté à Patreon, et envoie à Mangarr (jeton secret) :
  - les exports JSON des posts à télécharger  -> déposés dans <config>/patreon/<id>.json
  - la liste des derniers posts de chaque créateur suivi -> actus « nouveau chapitre sur Patreon ».
"""
import json
import os
import re
import secrets
import time

from mangarr.core.net import FetchError
from mangarr.sources import build_source
from mangarr.sources.patreon import inspect_export, media_candidates, post_id

FEED_KEY, CAMPAIGN_KEY, TOKEN_KEY = "patreon_feed", "patreon_campaigns", "patreon_token"
SYNC_KEY = "patreon_sync"
CAMPAIGN_API = ("https://www.patreon.com/api/posts?filter[campaign_id]={id}&sort=-published_at&page[count]=12"
                "&fields[post]=title,published_at,url&json-api-version=1.0")
MAX_FEED = 15
MAX_BODY = 25 * 1024 * 1024


def get_token(repo, renew=False):
    tok = repo.get_setting(TOKEN_KEY)
    if renew or not tok:
        tok = secrets.token_urlsafe(32)
        repo.set_setting(TOKEN_KEY, tok)
    return tok


def token_ok(repo, given):
    tok = repo.get_setting(TOKEN_KEY)
    return bool(tok and given) and secrets.compare_digest(str(given), tok)


def _series(ctx):
    """[(manga, source, params, {chap: post_url})] des séries Patreon suivies."""
    out = []
    for m in ctx.repo.list_mangas():
        if m["source"] != "patreon":
            continue
        try:
            src, params = build_source("patreon", dict(m["params"]), ctx.settings)
        except ValueError:
            continue
        out.append((m, src, params, {c: src.post_url(params, c) for c in src.explicit_chapters(params)}))
    return out


def _campaign_of(data):
    rel = ((data.get("data") or {}).get("relationships") or {}).get("campaign") or {}
    cid = str((rel.get("data") or {}).get("id") or "")
    return cid if cid.isdigit() else ""


def _campaign_map(ctx):
    """{id_du_post: id_de_campagne} : mémorisé à chaque export reçu, relu dans les exports déjà présents."""
    known = dict(ctx.repo.get_setting(CAMPAIGN_KEY, {}))
    for _, _, _, posts in _series(ctx):
        for url in posts.values():
            try:
                pid = post_id(url)
            except FetchError:
                continue
            if pid in known:
                continue
            try:
                with open(os.path.join(ctx.settings.patreon_dir, f"{pid}.json"), encoding="utf-8-sig") as f:
                    cid = _campaign_of(json.load(f))
            except (OSError, ValueError):
                continue
            if cid:
                known[pid] = cid
    return known


def wanted(ctx):
    """Ce dont Mangarr a besoin : posts à exporter (absents/expirés/invalides) et campagnes à surveiller."""
    need, camps = [], set()
    cmap = _campaign_map(ctx)
    for m, src, params, posts in _series(ctx):
        folder = m.get("folder") or os.path.join(ctx.settings.library_dir, m["name"])
        for chap, url in posts.items():
            try:
                pid = post_id(url)
            except FetchError:
                continue
            if pid in cmap:
                camps.add(cmap[pid])
            if os.path.exists(os.path.join(folder, src.file_pattern.format(name=m["name"], chap=chap))):
                continue
            status, _, _ = inspect_export(src.export_path(url))
            if status != "ok":
                need.append(pid)
    return {"posts": sorted(set(need)), "campaigns": sorted(camps), "campaign_api": CAMPAIGN_API}


def save_export(ctx, data, raw=None):
    """Valide et enregistre l'export d'API d'un post. Retourne (id, message) ; ValueError si invalide."""
    pid = str((data.get("data") or {}).get("id", "")) if isinstance(data, dict) else ""
    if not re.fullmatch(r"\d{6,}", pid):
        raise ValueError("ce n'est pas l'export d'API d'un post Patreon (id du post introuvable)")
    if not media_candidates(data):
        raise ValueError("aucune image dans cet export (post verrouillé pour votre compte ?)")
    os.makedirs(ctx.settings.patreon_dir, exist_ok=True)
    dest = os.path.join(ctx.settings.patreon_dir, f"{pid}.json")       # nom imposé : pas de traversée
    with open(dest + ".tmp", "wb") as out:
        out.write(raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8"))
    os.replace(dest + ".tmp", dest)
    return pid


def touch(ctx, **fields):
    """État de la synchro (script vu, dernier envoi) affiché sur la page Patreon."""
    st = dict(ctx.repo.get_setting(SYNC_KEY, {}))
    st.update(fields)
    ctx.repo.set_setting(SYNC_KEY, st)


def sync_status(ctx):
    return ctx.repo.get_setting(SYNC_KEY, {})


def inbox_dir(ctx):
    return os.path.join(ctx.settings.patreon_dir, "inbox")


def scan_inbox(ctx):
    """Un .json (n'importe quel nom) déposé dans <config>/patreon/inbox/ est validé, rangé sous le bon nom, puis retiré.
    Retourne (ids enregistrés, {fichier: erreur}). Un fichier invalide reste en place pour être corrigé."""
    folder, saved, errors = inbox_dir(ctx), [], {}
    try:
        names = sorted(f for f in os.listdir(folder) if f.lower().endswith(".json"))
    except OSError:
        return saved, errors
    cmap = dict(ctx.repo.get_setting(CAMPAIGN_KEY, {}))
    for fn in names:
        path = os.path.join(folder, fn)
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_BODY)
            data = json.loads(raw.decode("utf-8-sig"))
            pid = save_export(ctx, data, raw)
            if _campaign_of(data):
                cmap[pid] = _campaign_of(data)
            saved.append(pid)
            os.remove(path)
        except (ValueError, UnicodeDecodeError) as e:
            errors[fn] = str(e) if isinstance(e, ValueError) else "fichier non lisible (UTF-8 attendu)"
        except OSError as e:
            errors[fn] = str(e)
    ctx.repo.set_setting(CAMPAIGN_KEY, cmap)
    return saved, errors


def ingest(ctx, payload):
    """Traite l'envoi du userscript. Retourne {saved, errors, campaigns, mangas} ; ne lève pas sur un export isolé."""
    repo = ctx.repo
    saved, errors = [], {}
    cmap = dict(repo.get_setting(CAMPAIGN_KEY, {}))
    for pid, data in (payload.get("exports") or {}).items():
        try:
            real = save_export(ctx, data)
            saved.append(real)
            cid = _campaign_of(data)
            if cid:
                cmap[real] = cid
        except ValueError as e:
            errors[str(pid)[:20]] = str(e)
    repo.set_setting(CAMPAIGN_KEY, cmap)

    feed = dict(repo.get_setting(FEED_KEY, {}))
    for cid, posts in (payload.get("feeds") or {}).items():
        if not str(cid).isdigit() or not isinstance(posts, list):
            continue
        items = []
        for p in posts[:MAX_FEED]:
            if not isinstance(p, dict) or not re.fullmatch(r"\d{6,}", str(p.get("id", ""))):
                continue
            url = str(p.get("url") or "")
            items.append({"id": str(p["id"]), "title": str(p.get("title") or "")[:200],
                          "date": str(p.get("date") or "")[:10],
                          "url": url if url.startswith("https://www.patreon.com/") else
                          f"https://www.patreon.com/posts/{p['id']}"})
        feed[str(cid)] = {"fetched": time.time(), "posts": items}
    repo.set_setting(FEED_KEY, feed)
    if saved or payload.get("feeds"):
        touch(ctx, last_ingest=time.time(), last_saved=len(saved))
    mangas = sorted({m["name"] for m, _, _, posts in _series(ctx)
                     for u in posts.values() if _safe_pid(u) in saved})
    return {"saved": saved, "errors": errors, "campaigns": sorted(set(cmap.values())), "mangas": mangas}


def _safe_pid(url):
    try:
        return post_id(url)
    except FetchError:
        return None


def feed_for(ctx, manga):
    """Posts Patreon récents du créateur de cette série : [{id,title,date,url,new}] ; new = pas encore dans la série."""
    series = next((s for s in _series(ctx) if s[0]["id"] == manga["id"]), None)
    if not series:
        return []
    _, _, _, posts = series
    mine = {p for p in (_safe_pid(u) for u in posts.values()) if p}
    cmap = _campaign_map(ctx)
    feed = ctx.repo.get_setting(FEED_KEY, {})
    out, seen = [], set()
    for cid in {cmap[p] for p in mine if p in cmap}:
        for p in (feed.get(cid) or {}).get("posts", []):
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append({**p, "new": p["id"] not in mine})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def add_post(ctx, manga_id, post_url):
    """Ajoute un post à la série (numéro suivant). Retourne le numéro de tome."""
    m = ctx.repo.get_manga(manga_id)
    if not m or m["source"] != "patreon":
        raise ValueError("série Patreon introuvable")
    post_id(post_url)
    params = dict(m["params"])
    posts = params.get("posts") or {}
    if isinstance(posts, (list, tuple)):
        posts = {str(params.get("start_chap", 1) + i): u for i, u in enumerate(posts)}
    posts = dict(posts)
    if any(_safe_pid(u) == _safe_pid(post_url) for u in posts.values()):
        raise ValueError("ce post est déjà dans la série")
    num = max((int(k) for k in posts), default=0) + 1
    posts[str(num)] = post_url
    params["posts"] = posts
    ctx.repo.update_manga(manga_id, params=params)
    return num
