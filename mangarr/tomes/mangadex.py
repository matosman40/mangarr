"""API MangaDex : recherche, volumes (cache 12 h, repli sur cache périmé), jaquettes."""
import json
import os
import re
import time

import requests

from mangarr.core.net import MD_HEADERS, FetchError

MD_API = "https://api.mangadex.org"
CACHE_TTL = 12 * 3600


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _bad(r, api):
    snippet = re.sub(r"\s+", " ", r.text[:160])
    return FetchError(f"réponse non-JSON de {api} (HTTP {r.status_code}, {len(r.content)} octets, "
                      f"type {r.headers.get('Content-Type', '?')}) début : {snippet!r}")


def _titles(d):
    a = d["attributes"]
    return list(a.get("title", {}).values()) + [t for alt in a.get("altTitles", []) for t in alt.values()]


def _cover_url(d):
    for rel in d.get("relationships", []):
        if rel.get("type") == "cover_art" and rel.get("attributes", {}).get("fileName"):
            return f"https://uploads.mangadex.org/covers/{d['id']}/{rel['attributes']['fileName']}.512.jpg"
    return None


def search(session, title, api=MD_API, limit=10):
    """Liste de {id, title, cover_url, year} pour l'assistant d'ajout."""
    r = session.get(f"{api}/manga", params={"title": title, "limit": limit, "order[relevance]": "desc",
                                            "includes[]": "cover_art"},
                    headers=MD_HEADERS, timeout=(10, 30))
    r.raise_for_status()
    try:
        data = r.json().get("data", [])
    except ValueError:
        raise _bad(r, api)
    return [{"id": d["id"], "title": next(iter(d["attributes"].get("title", {}).values()), "?"),
             "cover_url": _cover_url(d), "year": d["attributes"].get("year")} for d in data], data


def cover_url(session, md_id, api=MD_API):
    r = session.get(f"{api}/manga/{md_id}", params={"includes[]": "cover_art"}, headers=MD_HEADERS,
                    timeout=(10, 30))
    r.raise_for_status()
    return _cover_url(r.json().get("data", {}))


def resolve_id(session, name, mangadex_id=None, mangadex_title=None, api=MD_API):
    """(id, titre, cover_url) : id fourni, sinon recherche par titre."""
    if mangadex_id:
        return mangadex_id, mangadex_title or name, None
    title = mangadex_title or name.replace("_", " ")
    shown, raw = search(session, title, api)
    if not raw:
        raise FetchError(f"MangaDex : aucun résultat pour '{title}'")
    best = next((i for i, d in enumerate(raw) if any(_norm(t) == _norm(title) for t in _titles(d))), 0)
    return shown[best]["id"], shown[best]["title"], shown[best]["cover_url"]


def volumes(session, cache_dir, key, name, params):
    """({volume: [chapitres entiers]}, titre, id). Cache 12 h ; repli sur le cache si l'API est KO."""
    api = params.get("mangadex_api", MD_API)
    path = os.path.join(cache_dir, f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(key))}.volumes.json")
    cache = None
    try:
        with open(path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    if cache and time.time() - cache.get("fetched", 0) < CACHE_TTL:
        return {int(k): v for k, v in cache["volumes"].items()}, cache.get("title"), cache.get("id")
    try:
        if not params.get("mangadex_id") and cache and cache.get("id"):
            mid, title = cache["id"], cache.get("title")
        else:
            mid, title, _ = resolve_id(session, name, params.get("mangadex_id"),
                                       params.get("mangadex_title"), api)
        r = session.get(f"{api}/manga/{mid}/aggregate", headers=MD_HEADERS, timeout=(10, 30))
        r.raise_for_status()
        try:
            raw = r.json().get("volumes", {})
        except ValueError:
            raise _bad(r, api)
        vols = {}
        for vk, v in (raw.items() if isinstance(raw, dict) else []):
            if not re.fullmatch(r"\d+", str(vk)):
                continue   # "none" = chapitres sans tome
            chs = v.get("chapters", {})
            keys = chs.keys() if isinstance(chs, dict) else [c.get("chapter") for c in chs]
            vols[int(vk)] = sorted(int(k) for k in keys if re.fullmatch(r"\d+", str(k)))  # décimaux ignorés
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"fetched": time.time(), "id": mid, "title": title,
                       "volumes": {str(k): v for k, v in vols.items()}}, f)
        return vols, title, mid
    except (requests.RequestException, ValueError, KeyError, FetchError) as e:
        if cache:
            return {int(k): v for k, v in cache["volumes"].items()}, cache.get("title"), cache.get("id")
        raise FetchError(f"MangaDex : {e}")
