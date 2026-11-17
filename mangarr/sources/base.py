"""Classe de base des sources et registre."""
import time

from mangarr.core.net import ChapterMissing, FetchError

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
