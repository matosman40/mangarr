from urllib.parse import quote, urlparse

import time


def fmt_ts(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "jamais"


COVER_HOST = "uploads.mangadex.org"


def cover_src(url):
    """Les jaquettes MangaDex passent par le serveur (cache, pas de blocage côté navigateur)."""
    if url and urlparse(url).netloc == COVER_HOST:
        return "/proxy/cover?u=" + quote(url, safe="")
    return url or ""
