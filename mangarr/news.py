"""Actus d'un manga suivi : fiche et auteurs (MyAnimeList via Jikan), news MAL et posts Reddit (r/manga).

Tout est mis en cache sur disque (6 h) et on retombe sur le cache périmé si une API est indisponible.
Chaque actu est un dict {src, kind, title, url, date, excerpt, meta, spoiler} : l'interface les affiche
dans une seule liste triée par date. Les discussions de chapitres et les posts Reddit marqués spoiler
sont signalés et masqués tant qu'on ne clique pas.
"""
import json
import os
import re
import time

import requests

from mangarr.core.net import MD_HEADERS, FetchError
from mangarr.titles import norm

JIKAN = "https://api.jikan.moe/v4"
CACHE_TTL = 6 * 3600
PAUSE = 0.4                  # Jikan limite à ~3 requêtes/s : on espace les appels
MAX_NEWS = 12
MAX_TOPICS = 10
MAX_AUTHORS = 3
MAX_REDDIT = 10
SUBREDDITS = ("manga",)
REDDIT_UA = "mangarr/0.1 (self-hosted news reader)"
VERSION = 2                  # change quand le format du cache évolue (force un rechargement)


def _cache_path(ctx, mid):
    return os.path.join(ctx.settings.cache_dir, f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(mid))}.news.json")


def _get(session, api, path, **params):
    r = session.get(f"{api}{path}", params=params or None, headers=MD_HEADERS, timeout=(10, 30))
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise FetchError(f"réponse non-JSON de Jikan (HTTP {r.status_code})")


def find_mal_id(session, api, name):
    """Id MyAnimeList du manga : meilleur titre exact, sinon premier résultat."""
    title = name.replace("_", " ")
    data = _get(session, api, "/manga", q=title, limit=8, order_by="popularity", sort="asc").get("data", [])
    if not data:
        return None
    for d in data:
        names = [d.get("title"), d.get("title_english"), *[t.get("title") for t in d.get("titles", [])]]
        if any(t and norm(t) == norm(title) for t in names):
            return d["mal_id"]
    return data[0]["mal_id"]


def mal_cover_url(session, api, name):
    """URL de la jaquette MyAnimeList (titre exact de préférence, sinon meilleur résultat) ou None."""
    title = name.replace("_", " ")
    data = _get(session, api, "/manga", q=title, limit=8, order_by="popularity", sort="asc").get("data", [])
    pick = next((d for d in data if any(t and norm(t) == norm(title) for t in
                 [d.get("title"), d.get("title_english"), *[x.get("title") for x in d.get("titles", [])]])),
                data[0] if data else None)
    return ((pick or {}).get("images", {}).get("jpg", {}) or {}).get("large_image_url")


def _text(s, n=400):
    s = re.sub(r"\s+", " ", s or "").strip()
    return (s[:n].rsplit(" ", 1)[0] + "…") if len(s) > n else s


_TOKEN = {}      # (url, client_id) -> (jeton, expiration) : le jeton Reddit dure 24 h


def reddit_token(session, st):
    """Jeton OAuth « application seule » de l'API officielle Reddit (client_credentials)."""
    key = (st.reddit_url, st.reddit_client_id)
    tok = _TOKEN.get(key)
    if tok and tok[1] > time.time() + 60:
        return tok[0]
    r = session.post(f"{st.reddit_url}/api/v1/access_token", auth=(st.reddit_client_id, st.reddit_client_secret),
                     data={"grant_type": "client_credentials"}, headers={"User-Agent": REDDIT_UA}, timeout=(10, 30))
    if r.status_code in (401, 403):
        raise FetchError("identifiants Reddit refusés (client id / secret invalides)")
    r.raise_for_status()
    j = r.json()
    if "access_token" not in j:
        raise FetchError("réponse Reddit sans jeton d'accès")
    _TOKEN[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


def reddit_posts(session, st, names):
    """Posts récents de r/manga via l'API officielle ; gardés seulement si leur titre contient un titre du manga."""
    if not (st.reddit_client_id and st.reddit_client_secret):
        raise FetchError("Reddit non configuré : renseignez MANGARR_REDDIT_CLIENT_ID et MANGARR_REDDIT_CLIENT_SECRET "
                         "(application « script » gratuite sur reddit.com/prefs/apps)")
    keys = {norm(n) for n in names if n and len(norm(n)) >= 3}
    out = []
    for sub in SUBREDDITS:
        r = session.get(f"{st.reddit_oauth_url}/r/{sub}/search",
                        headers={"Authorization": f"bearer {reddit_token(session, st)}", "User-Agent": REDDIT_UA},
                        params={"q": f'"{names[0]}"', "restrict_sr": 1, "sort": "new", "limit": 25, "raw_json": 1},
                        timeout=(10, 30))
        if r.status_code == 429:
            raise FetchError("limite de requêtes Reddit atteinte — réessayez plus tard")
        r.raise_for_status()
        for c in r.json().get("data", {}).get("children", []):
            d = c.get("data", {})
            if d.get("over_18") or not any(k in norm(d.get("title", "")) for k in keys):
                continue
            flair = d.get("link_flair_text") or ""
            out.append({"src": "reddit", "kind": "discussion" if "discussion" in flair.lower() else "news",
                        "title": d.get("title"), "url": "https://www.reddit.com" + d.get("permalink", ""),
                        "date": time.strftime("%Y-%m-%d", time.gmtime(d.get("created_utc") or 0)),
                        "excerpt": _text(d.get("selftext"), 240),
                        "meta": f"r/{sub} · {d.get('score', 0)} points · {d.get('num_comments', 0)} commentaires",
                        "spoiler": bool(d.get("spoiler")) or "spoil" in flair.lower()})
    return out[:MAX_REDDIT]


def fetch(ctx, session, name, mal_id=None):
    """Dictionnaire {mal_id, url, info, news, topics, authors, fetched} ; lève FetchError/RequestException."""
    api = ctx.settings.jikan_api
    if not mal_id:
        mal_id = find_mal_id(session, api, name)
        if not mal_id:
            raise FetchError(f"« {name} » est introuvable sur MyAnimeList")
        time.sleep(PAUSE)
    full = _get(session, api, f"/manga/{mal_id}/full").get("data", {})
    time.sleep(PAUSE)
    news = _get(session, api, f"/manga/{mal_id}/news").get("data", [])
    time.sleep(PAUSE)
    topics = _get(session, api, f"/manga/{mal_id}/forum").get("data", [])
    reddit, reddit_error = [], None
    try:
        names = [name.replace("_", " "), full.get("title"), full.get("title_english"),
                 *full.get("title_synonyms", [])]
        reddit = reddit_posts(session, ctx.settings, names)
    except (requests.RequestException, ValueError, FetchError) as e:     # Reddit ne doit jamais bloquer le reste
        reddit_error = str(e)
    authors = []
    for a in full.get("authors", [])[:MAX_AUTHORS]:
        time.sleep(PAUSE)
        try:
            p = _get(session, api, f"/people/{a['mal_id']}").get("data", {})
        except (requests.RequestException, FetchError):
            p = {}
        authors.append({"name": a.get("name"), "url": a.get("url"), "about": _text(p.get("about"), 500),
                        "image": (p.get("images", {}).get("jpg", {}) or {}).get("image_url")})
    return {
        "fetched": time.time(), "mal_id": mal_id, "url": full.get("url"),
        "info": {"title": full.get("title_english") or full.get("title"), "status": full.get("status"),
                 "chapters": full.get("chapters"), "volumes": full.get("volumes"),
                 "published": (full.get("published") or {}).get("string"), "score": full.get("score"),
                 "genres": [g["name"] for g in full.get("genres", []) + full.get("themes", [])],
                 "serializations": [s["name"] for s in full.get("serializations", [])]},
        "feed": sorted(
            [{"src": "mal", "kind": "news", "title": n.get("title"), "url": n.get("url"),
              "date": (n.get("date") or "")[:10], "excerpt": _text(n.get("excerpt")),
              "meta": n.get("author_username") or "MyAnimeList", "spoiler": False} for n in news[:MAX_NEWS]]
            + [{"src": "mal", "kind": "discussion", "title": t.get("title"), "url": t.get("url"),
                "date": (t.get("date") or "")[:10], "excerpt": "",
                "meta": f"forum MAL · {t.get('comments') or 0} réponses", "spoiler": True}
               for t in topics[:MAX_TOPICS]] + reddit,
            key=lambda x: x["date"], reverse=True),
        "reddit_error": reddit_error, "version": VERSION,
        "authors": authors,
    }


def get(ctx, session, manga, refresh=False):
    """(données, erreur) avec cache 6 h ; l'id MAL trouvé est mémorisé pour ne plus chercher."""
    path = _cache_path(ctx, manga["id"])
    cache = None
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    if cache and not refresh and cache.get("version") == VERSION and time.time() - cache.get("fetched", 0) < CACHE_TTL:
        return cache, None
    try:
        data = fetch(ctx, session, manga["name"], (cache or {}).get("mal_id") or manga["params"].get("mal_id"))
    except (requests.RequestException, ValueError, KeyError, FetchError) as e:
        return cache, f"{type(e).__name__} : {e}"
    os.makedirs(ctx.settings.cache_dir, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return data, None
