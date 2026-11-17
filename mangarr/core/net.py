"""Exceptions et utilitaires réseau (utilisables par les sources externes)."""
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
# L'API MangaDex veut un User-Agent de script et Accept: application/json (pas de faux navigateur).
MD_HEADERS = {"User-Agent": "mangarr/0.1 (self-hosted)", "Accept": "application/json"}


class ChapterMissing(Exception):
    """Le chapitre n'existe pas (encore) sur le site."""


class FetchError(Exception):
    """Erreur réseau / serveur : on abandonne le chapitre sans rien conserver."""


def sniff_image_ext(data):
    """Détecte le vrai format via les octets magiques (None si ce n'est pas une image)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def snippet(r, n=160):
    return re.sub(r"\s+", " ", r.text[:n]) if r.content else ""


def describe_response(r):
    return (f"HTTP {r.status_code}, {len(r.content)} octets, type {r.headers.get('Content-Type', '?')}, "
            f"début {snippet(r)!r}")


def fetch_image(session, url, referer=None):
    """Retourne ("ok", bytes) | ("missing", info) | ("error", info). 403 = erreur, jamais « absent »."""
    try:
        r = session.get(url, timeout=(10, 30), headers={"Referer": referer} if referer else None)
    except requests.RequestException as e:
        return "error", f"{e} ({url})"
    if r.status_code in (404, 410):
        return "missing", f"HTTP {r.status_code} ({url})"
    if r.status_code == 403:
        return "error", f"HTTP 403 : accès refusé (anti-bot, hotlink ou site changé ?) ({url})"
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code} ({url})"
    if sniff_image_ext(r.content) is None:
        return "missing", f"réponse non-image ({describe_response(r)}) ({url})"
    return "ok", r.content


def fetch_text(session, url, referer=None):
    """Texte d'une page. ChapterMissing si 404/410, FetchError sinon en cas d'échec."""
    try:
        r = session.get(url, timeout=(10, 30), headers={"Referer": referer} if referer else None)
    except requests.RequestException as e:
        raise FetchError(f"{e} ({url})")
    if r.status_code in (404, 410):
        raise ChapterMissing(f"HTTP {r.status_code} sur {url}")
    if r.status_code == 403:
        raise FetchError(f"HTTP 403 sur {url} : accès bloqué (anti-bot ?), ce n'est pas un chapitre absent")
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} sur {url}")
    return r.text


def make_session(retries=4):
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=retries, backoff_factor=1.5 if retries > 1 else 0.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    for scheme in ("http://", "https://"):
        s.mount(scheme, HTTPAdapter(max_retries=retry))
    return s
