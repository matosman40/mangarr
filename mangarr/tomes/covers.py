"""Jaquettes : MangaDex si trouvé, sinon page 1 du chapitre 1 (ou image fournie). cover.jpg dans la bibliothèque."""
import os
import re
import shutil
from zipfile import ZipFile

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
                shutil.copyfile(stored, dest)
    return stored
