"""Exemple de source externe. Copier dans <config>/plugins/ (ex. /config/plugins/monsite.py)."""
from mangarr.sources import ChapterMissing, FetchError, Source, fetch_image, fetch_text, register


@register("monsite")
class MonSite(Source):
    """Exemple : une API JSON qui liste les images d'un chapitre."""

    def iter_images(self, session, params, chap):
        # params contient les paramètres du manga (ex. "api_url": "https://exemple.invalid/api/{chap}")
        import json
        url = params["api_url"].format(chap=chap)
        data = json.loads(fetch_text(session, url))   # ChapterMissing si 404, FetchError si erreur
        if not data.get("images"):
            raise ChapterMissing(f"chapitre {chap} vide")
        for u in data["images"]:
            status, content = fetch_image(session, u, url)
            if status != "ok":
                raise FetchError(f"image {u} : {status} {content}")
            yield content
            self.sleep()
