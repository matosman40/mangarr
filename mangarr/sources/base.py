"""Classe de base des sources et registre."""
import html as htmllib
import re
import time
from urllib.parse import quote, urljoin

from mangarr.core.net import ChapterMissing, FetchError, fetch_text

MAX_RESULTS = 60

SOURCES = {}


def register(name):
    def deco(cls):
        cls.name = name
        SOURCES[name] = cls
        return cls
    return deco


class Source:
    """Une source sait lister/télécharger les images d'un chapitre.

    `defaults` est fusionné dans les paramètres de chaque manga qui l'utilise.
    """
    name = "?"
    defaults = {}
    file_pattern = "{name}_{chap}.cbz"   # nom du CBZ produit
    stop_on_duplicate = True             # sources « à tâtons » : 2 images identiques = fin du chapitre
    supports_tomes = True                # False : 1 post = 1 tome déjà complet
    in_library = False                   # True : le CBZ va directement dans la bibliothèque

    def __init__(self, params, page_delay=0.3):
        self.params = params
        self.page_delay = page_delay

    def explicit_chapters(self, params):
        """None = numérotation séquentielle ; sinon liste des numéros à traiter."""
        return None

    def iter_images(self, session, params, chap):
        """Génère les images (bytes) dans l'ordre. Voir ChapterMissing / FetchError."""
        raise NotImplementedError

    # ---- recherche / catalogue (optionnel) ---------------------------------- #
    def can_search(self):
        """Vrai si la source sait lister son catalogue (paramètres search_url + result_regex, ou méthode search)."""
        return type(self).search is not Source.search or bool(
            self.params.get("search_url") and self.params.get("result_regex"))

    def search(self, session, query, page=1):
        """Liste de {slug, title, cover?, url?}. `query` vide = parcourir le catalogue (page `page`).

        Par défaut : `search_url` (avec {q}, {page}, {domain}) et `result_regex` (groupes nommés slug, title, cover).
        Un plugin peut redéfinir cette méthode.
        """
        tpl, rx = self.params.get("search_url"), self.params.get("result_regex")
        if not tpl or not rx:
            raise NotImplementedError("cette source n'a pas de recherche")
        url = tpl.format(q=quote(query or ""), page=page, domain=self.params.get("domain", ""),
                         slug=self.params.get("slug", ""))
        try:
            text = fetch_text(session, url)
        except ChapterMissing:
            return []
        out, seen = [], set()
        for m in re.finditer(rx, text, re.S | re.I):
            g = m.groupdict()
            slug = (g.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            cover = (g.get("cover") or "").strip()
            out.append({"slug": htmllib.unescape(slug),
                        "title": htmllib.unescape((g.get("title") or slug).strip()),
                        "cover": urljoin(url, htmllib.unescape(cover)) if cover else None})
            if len(out) >= MAX_RESULTS:
                break
        return out

    def sleep(self):
        if self.page_delay:
            time.sleep(self.page_delay)

    def describe(self):
        return self.params.get("base_img_url") or self.params.get("chapter_url") or ""

    def probe(self, session, chap, limit=3):
        """Test rapide : nombre d'images trouvées (téléchargées jusqu'à `limit` si la source ne liste pas)."""
        n = 0
        for _ in self.iter_images(session, self.params, chap):
            n += 1
            if n >= limit and self.stop_on_duplicate:
                break
        return n


__all__ = ["Source", "register", "SOURCES", "ChapterMissing", "FetchError"]
