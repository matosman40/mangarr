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


# --------------------------------------------------------------------------- #
# Découverte : catalogue filtrable (genres, type, statut, tri) avec jaquettes et synopsis
# --------------------------------------------------------------------------- #
CACHE = {}   # (url, paramètres) -> (instant, json) ; vidé à la demande
FR_TAGS = {
    "Action": "Action", "Adventure": "Aventure", "Comedy": "Comédie", "Drama": "Drame", "Fantasy": "Fantastique",
    "Horror": "Horreur", "Mystery": "Mystère", "Psychological": "Psychologique", "Romance": "Romance",
    "Sci-Fi": "Science-fiction", "Slice of Life": "Tranche de vie", "Sports": "Sport", "Supernatural": "Surnaturel",
    "Tragedy": "Tragédie", "Historical": "Historique", "Martial Arts": "Arts martiaux", "School Life": "Vie scolaire",
    "Isekai": "Isekai", "Mecha": "Mecha", "Magic": "Magie", "Military": "Militaire", "Music": "Musique",
    "Monsters": "Monstres", "Survival": "Survie", "Villainess": "Anti-héroïne", "Reincarnation": "Réincarnation",
    "Crime": "Crime", "Thriller": "Thriller", "Superhero": "Super-héros", "Philosophical": "Philosophique",
    "Post-Apocalyptic": "Post-apocalyptique", "Medical": "Médical", "Cooking": "Cuisine", "Gore": "Gore",
    "Samurai": "Samouraï", "Ninja": "Ninja", "Vampires": "Vampires", "Zombies": "Zombies", "Time Travel": "Voyage dans le temps",
    "Video Games": "Jeux vidéo", "Virtual Reality": "Réalité virtuelle", "Animals": "Animaux", "Demons": "Démons",
}
ORDERS = {   # clé de tri MangaDex -> (libellé, sens)
    "latestUploadedChapter": ("Dernières sorties", "desc"), "followedCount": ("Populaires", "desc"),
    "rating": ("Mieux notés", "desc"), "createdAt": ("Récemment ajoutés", "desc"), "title": ("Titre A → Z", "asc"),
    "relevance": ("Pertinence", "desc"),
}
TYPES = {"ja": "Manga", "ko": "Manhwa", "zh": "Manhua"}
STATUSES = {"ongoing": "En cours", "completed": "Terminé", "hiatus": "En pause", "cancelled": "Annulé"}
DEMOS = {"shounen": "Shōnen", "seinen": "Seinen", "shoujo": "Shōjo", "josei": "Josei"}


def _cached_get(session, url, params, ttl):
    key = (url, tuple(params))
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    r = session.get(url, params=params, headers=MD_HEADERS, timeout=(10, 30))
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise _bad(r, url)
    CACHE[key] = (time.time(), data)
    return data


def _pick(d, *langs):
    """Texte localisé : langues préférées puis première valeur disponible."""
    for lang in langs:
        if d.get(lang):
            return d[lang]
    return next(iter(d.values()), "") if d else ""


def tag_list(session, api=MD_API):
    """[{id, name, label, group}] des genres et thèmes, triés (cache 24 h)."""
    data = _cached_get(session, f"{api}/manga/tag", [], 24 * 3600)
    out = []
    for t in data.get("data", []):
        a = t["attributes"]
        if a.get("group") in ("genre", "theme"):
            name = _pick(a.get("name", {}), "en")
            out.append({"id": t["id"], "name": name, "label": FR_TAGS.get(name, name), "group": a["group"]})
    return sorted(out, key=lambda x: (x["group"], x["label"].lower()))


def _normalize(d):
    a = d["attributes"]
    tags = [FR_TAGS.get(n, n) for t in a.get("tags", []) if t["attributes"].get("group") in ("genre", "theme")
            for n in [_pick(t["attributes"].get("name", {}), "en")]]
    return {"id": d["id"], "title": _pick(a.get("title", {}), "fr", "en"),
            "synopsis": (_pick(a.get("description", {}), "fr", "en") or "").strip(),
            "year": a.get("year"), "status": a.get("status"), "type": a.get("originalLanguage"),
            "demographic": a.get("publicationDemographic"), "rating": a.get("contentRating"),
            "tags": tags, "cover_url": _cover_url(d), "last_chapter": a.get("lastChapter")}


def discover(session, api=MD_API, q="", tags=(), all_tags=True, origin="", status=(), demo=(),
             order="followedCount", fr=False, adult=False, offset=0, limit=24):
    """(liste de mangas normalisés, total). Les contenus érotiques/pornographiques sont exclus sauf `adult`."""
    params = [("limit", limit), ("offset", max(0, offset)), ("includes[]", "cover_art"), ("hasAvailableChapters", "true")]
    if q:
        params.append(("title", q))
    for t in tags:
        params.append(("includedTags[]", t))
    if tags:
        params.append(("includedTagsMode", "AND" if all_tags else "OR"))
    if origin in TYPES:
        params.append(("originalLanguage[]", origin))
    params += [("status[]", s) for s in status if s in STATUSES]
    params += [("publicationDemographic[]", d) for d in demo if d in DEMOS]
    ratings = ["safe", "suggestive"] + (["erotica", "pornographic"] if adult else [])
    params += [("contentRating[]", r) for r in ratings]
    if fr:
        params.append(("availableTranslatedLanguage[]", "fr"))
    key = order if order in ORDERS else "followedCount"
    if q and key == "followedCount" and order not in ORDERS:
        key = "relevance"
    params.append((f"order[{key}]", ORDERS[key][1]))
    data = _cached_get(session, f"{api}/manga", params, 600)
    return [_normalize(d) for d in data.get("data", [])], data.get("total", 0)
