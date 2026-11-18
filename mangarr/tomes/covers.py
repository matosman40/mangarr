"""Jaquettes : MangaDex si trouvé, sinon page 1 du chapitre 1 (ou image fournie). cover.jpg dans la bibliothèque."""
import os
import re
import shutil
import threading
import time
from zipfile import ZipFile

from mangarr import news
from mangarr.core.net import fetch_image, sniff_image_ext
from mangarr.tomes import mangadex


def _first_page(folder, tomes_dir, name):
    """Première image du plus petit tome ou chapitre disponible."""
    cands = []
    for d, pat in ((tomes_dir, rf"^{re.escape(name)}_T(\d+)\.cbz$"), (folder, rf"^{re.escape(name)}_(\d+)\.cbz$")):
        if os.path.isdir(d):
            cands += sorted(((int(m.group(1)), os.path.join(d, f)) for f in os.listdir(d)
                             if (m := re.match(pat, f))))
    for _, path in cands:
        with ZipFile(path) as z:
            imgs = sorted(n for n in z.namelist() if n.lower() != "comicinfo.xml")
            if imgs:
                return z.read(imgs[0])
    return None


def ensure_cover(ctx, manga, folder, tomes_dir, md_id=None, md_api=mangadex.MD_API):
    """Garantit config/covers/<id>.jpg et sa copie <dossier du manga>/cover.jpg. Retourne le chemin ou None."""
    os.makedirs(ctx.settings.covers_dir, exist_ok=True)
    stored = manga.get("cover") or os.path.join(ctx.settings.covers_dir, f"{manga['id']}.jpg")
    if not os.path.isfile(stored):
        data = None
        if md_id:
            try:
                url = mangadex.cover_url(ctx.session, md_id, md_api)
                if url:
                    st, d = fetch_image(ctx.session, url)
                    data = d if st == "ok" else None
            except Exception:
                data = None
        if data is None:
            data = _first_page(folder, tomes_dir, manga["name"])
        if data is None or sniff_image_ext(data) is None:
            return None
        with open(stored, "wb") as f:
            f.write(data)
        ctx.repo.update_manga(manga["id"], cover=stored)
    for d in {tomes_dir, folder}:
        if os.path.isdir(d):
            dest = os.path.join(d, "cover.jpg")
            if not os.path.exists(dest):
                try:
                    shutil.copyfile(stored, dest)
                except OSError:       # dossier en lecture seule : la jaquette reste dans /config, sans bloquer
                    pass
    return stored


COVER_PREFIXES = ("https://uploads.mangadex.org/", "https://cdn.myanimelist.net/")


def store_md_cover(ctx, mid, url):
    """Télécharge la jaquette choisie (uniquement depuis uploads.mangadex.org) et l'associe au manga."""
    if not url.startswith(COVER_PREFIXES):
        return False
    st, data = fetch_image(ctx.fast_session, url)
    if st != "ok" or sniff_image_ext(data) is None:
        return False
    os.makedirs(ctx.settings.covers_dir, exist_ok=True)
    path = os.path.join(ctx.settings.covers_dir, f"{mid}.jpg")
    with open(path, "wb") as f:
        f.write(data)
    ctx.repo.update_manga(mid, cover=path)
    return True


# --------------------------------------------------------------------------- #
# Jaquettes manquantes : on les complète automatiquement
# --------------------------------------------------------------------------- #
LOCAL_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png", "poster.jpg")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
_RUN = threading.Lock()
_LAST = {"t": 0.0}
MIN_INTERVAL = 120          # secondes entre deux passes automatiques


def _dirs(ctx, m):
    tomes = m.get("tomes_folder") or os.path.join(ctx.settings.library_dir, m["name"])
    chapters = m.get("folder") or os.path.join(ctx.settings.chapters_dir, m["name"])
    return [tomes, chapters]


def _local_file(dirs):
    """Jaquette déjà présente dans le dossier (Komga, Kavita, ancien script…)."""
    for d in dirs:
        for n in LOCAL_NAMES:
            p = os.path.join(d, n)
            try:
                with open(p, "rb") as f:
                    data = f.read(8 * 1024 * 1024)
            except OSError:
                continue
            if sniff_image_ext(data):
                return data
    return None


def _first_archive_image(dirs):
    """Première page du plus petit tome (ou chapitre) disponible."""
    from mangarr import library_scan
    for d in dirs:
        if not os.path.isdir(d):
            continue
        files = [f for f in os.listdir(d) if f.lower().endswith((".cbz", ".zip"))]
        files.sort(key=lambda f: (library_scan.volume_number(f) is None, library_scan.volume_number(f) or 0, f.lower()))
        for f in files[:3]:
            try:
                with ZipFile(os.path.join(d, f)) as z:
                    names = sorted(n for n in z.namelist() if n.lower().endswith(IMAGE_EXT))
                    data = z.read(names[0]) if names else None
            except (OSError, ValueError, KeyError):
                continue
            if data and sniff_image_ext(data):
                return data
    return None


def _md_url_for_name(ctx, m):
    """Jaquette MangaDex retrouvée par le nom du manga (titre suffisamment proche)."""
    from mangarr.titles import score
    try:
        items, _ = mangadex.discover(ctx.fast_session, ctx.settings.mangadex_api, q=m["name"].replace("_", " "),
                                     order="relevance", limit=5)
    except Exception:
        return None
    best = max(items, key=lambda it: max((score(t, m["name"]) for t in [it["title"]] + it["alt_titles"]), default=0),
               default=None)
    if best and max(score(t, m["name"]) for t in [best["title"]] + best["alt_titles"]) >= 0.88:
        return best["cover_url"]
    return None


def _save(ctx, m, data):
    os.makedirs(ctx.settings.covers_dir, exist_ok=True)
    path = os.path.join(ctx.settings.covers_dir, f"{m['id']}.jpg")
    with open(path, "wb") as f:
        f.write(data)
    ctx.repo.update_manga(m["id"], cover=path)


def fill_missing(ctx, limit=30):
    """Donne une jaquette à chaque manga qui n'en a pas. Retourne le nombre de jaquettes ajoutées.

    Ordre : fichier de jaquette du dossier, jaquette MangaDex (id connu), première page d'un tome/chapitre,
    puis jaquette MangaDex retrouvée par le nom.
    """
    done = 0
    for m in ctx.repo.list_mangas():
        if done >= limit:
            break
        if m.get("cover") and os.path.isfile(m["cover"]):
            continue
        dirs = _dirs(ctx, m)
        data = _local_file(dirs)
        if data:
            _save(ctx, m, data)
            done += 1
            continue
        md_id = m["params"].get("mangadex_id")
        why = []
        if md_id:
            try:
                url = mangadex.cover_url(ctx.fast_session, md_id, ctx.settings.mangadex_api)
                if url and store_md_cover(ctx, m["id"], url):
                    done += 1
                    continue
                why.append("MangaDex : pas de jaquette")
            except Exception as e:
                why.append(f"MangaDex : {type(e).__name__}")
        data = _first_archive_image(dirs)
        if data:
            _save(ctx, m, data)
            done += 1
            continue
        if not md_id:
            url = _md_url_for_name(ctx, m)
            if url and store_md_cover(ctx, m["id"], url):
                done += 1
                continue
            why.append("MangaDex : titre introuvable")
        try:                                         # dernier recours : MyAnimeList
            url = news.mal_cover_url(ctx.fast_session, ctx.settings.jikan_api, m["name"])
            if url and store_md_cover(ctx, m["id"], url):
                done += 1
                continue
            why.append("MyAnimeList : pas de jaquette")
        except Exception as e:
            why.append(f"MyAnimeList : {type(e).__name__}")
        ctx.repo.add_log("warn", "jaquette introuvable (" + " ; ".join(why) + ")", m["id"])
    return done


def ensure_async(ctx, force=False):
    """Lance `fill_missing` en arrière-plan si des jaquettes manquent (au plus une passe toutes les 2 min)."""
    if _RUN.locked() or (not force and time.time() - _LAST["t"] < MIN_INTERVAL):
        return False
    if all(m.get("cover") and os.path.isfile(m["cover"]) for m in ctx.repo.list_mangas()):
        return False
    _LAST["t"] = time.time()

    def work():
        with _RUN:
            try:
                fill_missing(ctx)
            except Exception as e:
                ctx.repo.add_log("warn", f"jaquettes : {type(e).__name__}")

    threading.Thread(target=work, daemon=True, name="covers-fill").start()
    return True
