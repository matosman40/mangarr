"""Sources génériques : pattern (images numérotées) et html (page par chapitre), et presets."""
import html as htmllib
import re
from urllib.parse import quote, urljoin

from mangarr.core.net import ChapterMissing, FetchError, fetch_image, fetch_text
from mangarr.sources.base import Source, register

EXTENSIONS = (".jpg", ".png", ".jpeg", ".webp")


@register("pattern")
class PatternSource(Source):
    """Images en <base_img_url>/<chapitre>/<page><ext>, jusqu'au premier 404."""

    def _base(self, params):
        if params.get("base_img_url"):
            base = params["base_img_url"]
            if "{" in base:
                base = base.format(slug=params.get("slug", ""), domain=params.get("domain", ""))
        else:
            base = params["base_template"].format(slug=params.get("slug", ""), domain=params.get("domain", ""))
        return quote(base, safe=":/%")  # un %20 déjà présent n'est pas ré-encodé

    @staticmethod
    def _fmt(n, fmt):
        return f"{n:02}" if fmt == "02" else str(n)

    def _probe_first(self, session, base, chap, params):
        pref = (params.get("page_start", 0), params.get("page_format", "02"))
        variants = [pref] + [v for v in [(0, "02"), (1, "02"), (1, "d"), (0, "d")] if v != pref]
        first_url = info = None
        for start, fmt in variants:
            for ext in EXTENSIONS:
                url = f"{base}/{chap}/{self._fmt(start, fmt)}{ext}"
                st, data = fetch_image(session, url, base)
                if st == "ok":
                    return start, fmt, ext, data
                if st == "error":
                    raise FetchError(data)
                first_url = first_url or url
                if info is None or ("non-image" in data and "non-image" not in info):
                    info = data   # on garde l'indice le plus utile (réponse non-image > simple 404)
        raise ChapterMissing(f"{info} - ex. {first_url}")

    def iter_images(self, session, params, chap):
        base = self._base(params)
        start, fmt, ext, data = self._probe_first(session, base, chap, params)
        params["page_start"], params["page_format"] = start, fmt
        page = start
        while True:
            yield data
            page += 1
            self.sleep()
            data = None
            for e in [ext] + [x for x in EXTENSIONS if x != ext]:
                st, res = fetch_image(session, f"{base}/{chap}/{self._fmt(page, fmt)}{e}", base)
                if st == "ok":
                    data, ext = res, e
                    break
                if st == "error":
                    raise FetchError(f"page {page} : {res}")
            if data is None:
                return  # fin normale du chapitre


@register("html")
class HtmlSource(Source):
    """Une page HTML par chapitre contenant les images.

    Paramètres : chapter_url ("...{chap}..."), img_regex (1 groupe = URL), img_filter (regex que l'URL contient).
    """
    stop_on_duplicate = False  # liste d'images connue d'avance
    IMG_TAG = re.compile(
        r"""<img[^>]+?(?:data-src|data-lazy-src|data-original|src)\s*=\s*["']\s*([^"']+?)\s*["']""", re.I)
    IMG_ANY = re.compile(
        r"""["'(]((?:https?:)?\\?/\\?/[^"'\s)]+?\.(?:jpe?g|png|webp)(?:\?[^"'\s)]*)?"""
        r"""|\\?/[^"'\s)]+?\.(?:jpe?g|png|webp))""", re.I)

    @staticmethod
    def _extract(text, page_url, params, regexes):
        urls, seen = [], set()
        for rx in regexes:
            for u in rx.findall(text):
                if isinstance(u, tuple):
                    u = u[0]
                u = htmllib.unescape(u).replace("\\/", "/").strip()
                u = urljoin(page_url, u)
                if not re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I):
                    continue
                if params.get("img_filter") and not re.search(params["img_filter"], u):
                    continue
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            if urls:
                break
        return urls

    def list_image_urls(self, session, params, chap):
        page_url = params["chapter_url"].format(chap=chap, slug=params.get("slug", ""),
                                                domain=params.get("domain", ""))
        text = fetch_text(session, page_url)
        regexes = [re.compile(params["img_regex"])] if params.get("img_regex") else [self.IMG_TAG, self.IMG_ANY]
        urls = self._extract(text, page_url, params, regexes)
        if not urls:
            raise ChapterMissing(f"aucune image trouvée sur {page_url} ({len(text)} caractères reçus)")
        return page_url, urls

    def iter_images(self, session, params, chap):
        page_url, urls = self.list_image_urls(session, params, chap)
        for i, u in enumerate(urls, 1):
            st, data = fetch_image(session, u, page_url)
            if st != "ok":   # le chapitre est listé : une image manquante = erreur, pas une fin
                raise FetchError(f"image {i}/{len(urls)} ({st}: {data})")
            yield data
            self.sleep()

    def probe(self, session, chap, limit=3):
        return len(self.list_image_urls(session, self.params, chap)[1])


# ---- Presets : seul "slug" (+ réglages de pages) est à fournir ---- #
@register("lelscans")
class LelScans(PatternSource):
    """lelscans : images en <base>/<slug>/<chap>/<page>"""
    defaults = {"base_template": "https://lelscans.net/mangas/{slug}", "page_start": 0, "page_format": "02"}


@register("lelscanfr")
class LelScanFr(HtmlSource):
    """lelscanfr : page par chapitre, images dans /storage/content/"""
    defaults = {"chapter_url": "https://{domain}/manga/{slug}/{chap}", "domain": "www.lelscanfr.com",
                "img_filter": r"/storage/content/"}


@register("animesama")
class AnimeSama(PatternSource):
    """anime-sama : images en /s2/scans/<slug>/<chap>/<page>"""
    defaults = {"base_template": "https://{domain}/s2/scans/{slug}", "domain": "anime-sama.fr",
                "page_start": 1, "page_format": "d"}
