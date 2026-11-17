"""Source Patreon : exports JSON faits depuis le navigateur de l'utilisateur.

Patreon bloque les requêtes serveur (403, Cloudflare) : on ne tente PAS de contourner la protection.
Chaque post = un tome. Export attendu dans <config>/patreon/<id_du_post>.json
"""
import json
import os
import re
import time

from mangarr.core.net import ChapterMissing, FetchError, fetch_image
from mangarr.sources.base import Source, register

API = ("https://www.patreon.com/api/posts/{id}?include=images,media"
       "&fields[post]=title,post_metadata,post_type"
       "&fields[media]=download_url,file_name,image_urls,mimetype&json-api-version=1.0")


def post_id(url):
    m = re.search(r"(\d{6,})(?:[/?#]|$)", str(url))
    if not m:
        raise FetchError(f"id de post introuvable dans l'URL : {url}")
    return m.group(1)


def media_candidates(data):
    """Pour chaque image, [fichier d'origine, variante] dans l'ordre du post."""
    media = {m["id"]: m.get("attributes", {}) for m in data.get("included", []) if m.get("type") == "media"}
    attrs = data.get("data", {}).get("attributes", {}) or {}
    order = (attrs.get("post_metadata") or {}).get("image_order") or []
    if not order:
        rel = (data.get("data", {}).get("relationships", {}).get("images", {}) or {}).get("data") or []
        order = [r["id"] for r in rel]
    out = []
    for mid in order:
        a = media.get(str(mid))
        if not a:
            continue
        cands = [u for u in (a.get("download_url"), (a.get("image_urls") or {}).get("original")) if u]
        if cands:
            out.append(cands)
    return out


def url_expiry(url):
    m = re.search(r"token-time=(\d+)", url)
    return int(m.group(1)) if m else None


def export_expiry(path):
    """Date d'expiration (epoch) la plus tardive des liens d'un export, ou None."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            cands = media_candidates(json.load(f))
    except (OSError, ValueError):
        return None
    exps = [e for c in cands for u in c if (e := url_expiry(u))]
    return max(exps) if exps else None


@register("patreon")
class PatreonSource(Source):
    """Posts Patreon (1 post = 1 tome) via exports JSON déposés dans <config>/patreon/."""
    supports_tomes = False
    stop_on_duplicate = False
    in_library = True
    file_pattern = "{name}_T{chap:02}.cbz"
    export_dir = None   # renseigné par le moteur (settings.patreon_dir)

    def explicit_chapters(self, params):
        posts = params.get("posts") or {}
        if isinstance(posts, (list, tuple)):
            return [params.get("start_chap", 1) + i for i in range(len(posts))]
        return sorted(int(k) for k in posts)

    def post_url(self, params, chap):
        posts = params.get("posts") or {}
        if isinstance(posts, (list, tuple)):
            i = chap - params.get("start_chap", 1)
            return posts[i] if 0 <= i < len(posts) else None
        return posts.get(str(chap)) or posts.get(chap)

    def export_path(self, post):
        return os.path.join(self.export_dir or "", f"{post_id(post)}.json")

    def iter_images(self, session, params, chap):
        post = self.post_url(params, chap)
        if not post:
            raise ChapterMissing(f"aucun post défini pour le tome {chap}")
        path = self.export_path(post)
        if not os.path.isfile(path):
            raise FetchError(
                f"export manquant pour le tome {chap} : {path}\n"
                f"      -> ouvre dans ton navigateur (connecté à Patreon) : {API.format(id=post_id(post))}\n"
                "      -> enregistre la page en .json à cet emplacement (ou dépose-la dans l'interface)")
        try:
            with open(path, encoding="utf-8-sig") as f:
                items = media_candidates(json.load(f))
        except ValueError:
            raise FetchError(f"{path} n'est pas un JSON valide (enregistre la page d'API, pas celle du post)")
        except OSError as e:
            raise FetchError(str(e))
        if not items:
            raise FetchError(f"aucune image dans {path} (post verrouillé pour ce compte ?)")
        exps = [e for c in items for u in c if (e := url_expiry(u))]
        if exps and max(exps) < time.time():
            when = time.strftime("%d/%m/%Y %H:%M", time.localtime(max(exps)))
            raise FetchError(f"export expiré depuis le {when} : refais l'export de ce post (liens signés)")
        for i, cands in enumerate(items, 1):
            err = None
            for u in cands:
                st, data = fetch_image(session, u, "https://www.patreon.com/")
                if st == "ok":
                    break
                err = f"{st}: {data}"
            else:
                raise FetchError(f"image {i}/{len(items)} ({err})"
                                 + (" - lien signé expiré ? refais l'export." if "403" in str(err) else ""))
            yield data
            self.sleep()

    def probe(self, session, chap, limit=3):
        post = self.post_url(self.params, chap)
        if not post:
            raise ChapterMissing(f"aucun post défini pour le tome {chap}")
        path = self.export_path(post)
        if not os.path.isfile(path):
            raise FetchError(f"export manquant : {path}")
        with open(path, encoding="utf-8-sig") as f:
            return len(media_candidates(json.load(f)))
