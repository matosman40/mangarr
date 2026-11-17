"""Recherche générique : extraction automatique des résultats d'une page de site et détection de l'adresse de recherche."""
import html as htmllib
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlparse

from mangarr.core.net import ChapterMissing, FetchError, fetch_text

A_TAG = re.compile(r"<a\b([^>]*?)>(.*?)</a>", re.S | re.I)
IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)
TAGS = re.compile(r"<[^>]+>")
HEADING = re.compile(r"<(h[1-6]|p|span|strong|div)\b[^>]*>([^<]{3,120})</\1>", re.I)
CHAPTER_SLUG = re.compile(r"^(chapter|chapitre|chap|ch|scan|episode|ep|read)[-_ ]?\d", re.I)
SKIP_SEGMENTS = {"genre", "genres", "tag", "tags", "category", "categories", "author", "authors", "page", "login",
                 "register", "feed", "search", "recherche", "contact", "wp-content", "wp-json", "user", "users",
                 "random", "latest", "popular", "bookmarks", "history", "blog", "news", "about", "dmca", "privacy"}
MAX_CARDS = 60

# modèles d'adresse de recherche courants ({q} = texte, {page} ajouté quand le site le permet)
CANDIDATES = [
    "/?s={q}&post_type=wp-manga",          # WordPress / Madara
    "/?s={q}",                             # WordPress, MangaThemesia
    "/manga?title={q}&page={page}",
    "/manga?search={q}",
    "/catalogue/?search={q}&page={page}",
    "/catalogue?search={q}",
    "/search?q={q}",
    "/search?query={q}",
    "/search?keyword={q}",
    "/recherche?q={q}",
    "/?q={q}",
]


def _attr(attrs, name):
    m = re.search(rf'(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', attrs, re.I)
    return htmllib.unescape(m.group(1) or m.group(2) or "").strip() if m else ""


def _same_site(url, base):
    host = lambda u: urlparse(u).netloc.lower().removeprefix("www.")
    return host(url) == host(base)


def extract_cards(page, page_url):
    """[{slug, title, cover}] des cartes « jaquette + lien » d'une page (liste ou résultats de recherche)."""
    out, seen = [], set()
    for m in A_TAG.finditer(page):
        attrs, inner = m.group(1), m.group(2)
        href = _attr(attrs, "href")
        img = IMG_TAG.search(inner)
        if not href or not img or len(inner) > 6000 or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(page_url, href)
        if not _same_site(url, page_url):
            continue
        path = [seg for seg in urlparse(url).path.split("/") if seg]
        if not path or any(seg.lower() in SKIP_SEGMENTS for seg in path[:-1]) or path[-1].lower() in SKIP_SEGMENTS:
            continue
        slug = re.sub(r"\.(html?|php)$", "", path[-1])
        if len(slug) < 2 or CHAPTER_SLUG.match(slug) or url.rstrip("/") == page_url.split("?")[0].rstrip("/"):
            continue
        cover = ""
        for key in ("data-src", "data-lazy-src", "data-original", "src"):
            cover = _attr(img.group(1), key)
            if cover and not cover.startswith("data:"):
                break
            cover = ""
        if any(w in cover.lower() for w in ("logo", "avatar", "banner", "sprite", "placeholder")) and not path:
            continue
        title = _attr(attrs, "title") or _attr(img.group(1), "alt") or _attr(img.group(1), "title")
        if not title:
            h = HEADING.search(inner)
            title = htmllib.unescape(h.group(2)).strip() if h else ""
        if not title:
            title = re.sub(r"\s+", " ", htmllib.unescape(TAGS.sub(" ", inner))).strip()[:120]
        if not title or slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "title": title, "cover": urljoin(url, cover) if cover else None})
        if len(out) >= MAX_CARDS:
            break
    return out


def search_page(session, url_template, query, page=1, domain="", slug=""):
    """Résultats pour une adresse modèle ; page > 1 sans {page} dans le modèle = plus de résultats."""
    if page > 1 and "{page}" not in url_template:
        return []
    url = url_template.format(q=quote(query or ""), page=page, domain=domain, slug=slug)
    try:
        text = fetch_text(session, url)
    except ChapterMissing:
        return []
    return extract_cards(text, url)


def _try(session, base, template, probe, nonsense):
    url = base + template
    try:
        hits = search_page(session, url, probe)
        noise = search_page(session, url, nonsense)
    except (FetchError, ValueError):
        return template, 0, []
    # une vraie recherche renvoie moins de résultats pour une requête absurde que pour une requête courante
    if not hits or len({h["slug"] for h in hits} - {h["slug"] for h in noise}) == 0:
        return template, 0, []
    return template, len(hits) - len(noise), hits


def detect(session, site_url, probe="one"):
    """(modèle d'adresse complet, résultats d'exemple) pour un site, ou (None, []) si rien de fiable n'est trouvé."""
    parts = urlparse(site_url if "://" in site_url else "https://" + site_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("adresse de site invalide")
    base = f"{parts.scheme}://{parts.netloc}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        tried = list(pool.map(lambda t: _try(session, base, t, probe, "zzqxjkw"), CANDIDATES))
    tried = [t for t in tried if t[1] > 0]
    if not tried:
        return None, []
    template, _, hits = max(tried, key=lambda t: t[1])
    return base + template, hits
