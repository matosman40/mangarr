"""Traduction des résumés en français (MyMemory gratuit, ou LibreTranslate auto-hébergé), avec cache disque."""
import hashlib
import html
import os
import re
import time

import requests

MYMEMORY = "https://api.mymemory.translated.net/get"
PROVIDERS = {"mymemory": "MyMemory (gratuit, quota quotidien limité)",
             "libretranslate": "LibreTranslate (auto-hébergé, sans limite)",
             "none": "Désactivée"}
CHUNK = 450                  # MyMemory : 500 caractères maximum par requête
BLOCK_SECONDS = 3600
_blocked_until = 0.0         # après un quota atteint, on ne réessaie pas pendant une heure


class TranslateError(Exception):
    pass


def get_config(repo):
    cfg = dict(repo.get_setting("translation", {}))
    cfg.setdefault("provider", "mymemory")
    return cfg


def chunks(text, size=CHUNK):
    """Découpe aux fins de phrase/ligne, sans dépasser `size` caractères."""
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.!?。！？\n])\s+", text.strip()):
        while len(piece) > size:                       # phrase interminable : coupe dure
            cut = piece.rfind(" ", 0, size) or size
            cut = cut if cut > 0 else size
            parts.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if len(cur) + len(piece) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        cur = f"{cur} {piece}".strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def _mymemory(session, cfg, text, src):
    global _blocked_until
    if time.time() < _blocked_until:
        raise TranslateError("quota MyMemory atteint : réessayez plus tard ou configurez LibreTranslate (Réglages)")
    base = cfg.get("mymemory_url") or MYMEMORY
    out = []
    for part in chunks(text):
        r = session.get(base, params={"q": part, "langpair": f"{src}|fr"}, timeout=(5, 15))
        try:
            data = r.json()
        except ValueError:
            raise TranslateError(f"MyMemory : réponse illisible (HTTP {r.status_code})")
        status = int(data.get("responseStatus", r.status_code) or 0)
        translated = str((data.get("responseData") or {}).get("translatedText", ""))
        if status == 429 or data.get("quotaFinished") or "MYMEMORY WARNING" in translated.upper():
            _blocked_until = time.time() + BLOCK_SECONDS
            raise TranslateError("quota MyMemory atteint : réessayez plus tard ou configurez LibreTranslate (Réglages)")
        if status != 200 or not translated:
            raise TranslateError(f"MyMemory : {data.get('responseDetails') or 'erreur'}")
        out.append(html.unescape(translated))
    return " ".join(out)


def _libretranslate(session, cfg, text, src):
    url = (cfg.get("url") or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise TranslateError("adresse LibreTranslate invalide (Réglages)")
    body = {"q": text, "source": "auto", "target": "fr", "format": "text"}
    if cfg.get("api_key"):
        body["api_key"] = cfg["api_key"]
    r = session.post(f"{url}/translate", json=body, timeout=(5, 30))
    try:
        data = r.json()
    except ValueError:
        raise TranslateError(f"LibreTranslate : réponse illisible (HTTP {r.status_code})")
    if r.status_code != 200 or "translatedText" not in data:
        raise TranslateError(f"LibreTranslate : {data.get('error', 'HTTP ' + str(r.status_code))}")
    return data["translatedText"]


def translate(ctx, text, src="en"):
    """(texte français, None) ou (None, message d'erreur). Résultat mis en cache sur disque."""
    text = (text or "").strip()
    if not text:
        return "", None
    cfg = get_config(ctx.repo)
    provider = cfg["provider"]
    if provider not in ("mymemory", "libretranslate"):
        return None, "traduction désactivée"
    src = (src or "en").split("-")[0].lower()
    key = hashlib.sha1(f"{src}\0{text}".encode()).hexdigest()
    path = os.path.join(ctx.settings.cache_dir, "translations", key)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except OSError:
        pass
    try:
        fn = _mymemory if provider == "mymemory" else _libretranslate
        result = fn(ctx.fast_session, cfg, text, src).strip()
    except TranslateError as e:
        return None, str(e)
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        return None, f"service de traduction injoignable ({type(e).__name__})"   # sans adresse ni clé
    if not result:
        return None, "traduction vide"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(result)
    os.replace(path + ".tmp", path)
    return result, None


def reset_block():
    global _blocked_until
    _blocked_until = 0.0
