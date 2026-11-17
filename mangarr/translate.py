"""Traduction des résumés en français (MyMemory gratuit, ou LibreTranslate auto-hébergé), avec cache disque."""
import hashlib
import html
import os
import re
import time

import requests

MYMEMORY = "https://api.mymemory.translated.net/get"
GOOGLE = "https://translate.googleapis.com/translate_a/single"
PROVIDERS = {"google": "Google Traduction (non officiel, sans clé, rapide)",
             "mymemory": "MyMemory (gratuit, quota quotidien limité)",
             "libretranslate": "LibreTranslate (auto-hébergé, sans limite)",
             "none": "Désactivée"}
CHUNK = 450                  # MyMemory : 500 caractères maximum par requête
BLOCK_SECONDS = 3600
SHORT_BLOCK = 120            # panne réseau : courte pause
LABELS = {"google": "Google Traduction", "mymemory": "MyMemory", "libretranslate": "LibreTranslate"}
_blocked = {}                # service -> instant de reprise ; après un quota atteint, pause d'une heure


class TranslateError(Exception):
    pass


def get_config(repo):
    """Réglages ; sans configuration enregistrée : Google avec repli automatique sur MyMemory."""
    stored = repo.get_setting("translation", None)
    cfg = dict(stored or {})
    if not stored:
        cfg["fallback"] = True
    cfg.setdefault("provider", "google")
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


def _is_blocked(name):
    return time.time() < _blocked.get(name, 0)


def _block(name, seconds=None):
    _blocked[name] = time.time() + (seconds or BLOCK_SECONDS)


def _google(session, cfg, text, src):
    if _is_blocked("google"):
        raise TranslateError("Google Traduction temporairement indisponible (limite atteinte)")
    out = []
    for part in chunks(text, 1200):
        r = session.get(cfg.get("google_url") or GOOGLE,
                        params={"client": "gtx", "sl": src or "auto", "tl": "fr", "dt": "t", "q": part},
                        timeout=(4, 10))
        if r.status_code == 429:
            _block("google")
            raise TranslateError("Google Traduction : limite atteinte")
        if r.status_code != 200:
            raise TranslateError(f"Google Traduction : HTTP {r.status_code}")
        try:
            segments = r.json()[0]
            translated = "".join(seg[0] for seg in segments if seg and seg[0])
        except (ValueError, IndexError, TypeError, KeyError):
            raise TranslateError("Google Traduction : réponse illisible")
        if not translated.strip():
            raise TranslateError("Google Traduction : traduction vide")
        out.append(translated)
    return " ".join(out)


def _mymemory(session, cfg, text, src):
    if _is_blocked("mymemory"):
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
            _block("mymemory")
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


def _cache_path(ctx, text, src):
    key = hashlib.sha1(f"{(src or 'en').split('-')[0].lower()}\0{text.strip()}".encode()).hexdigest()
    return os.path.join(ctx.settings.cache_dir, "translations", key)


def cached(ctx, text, src="en"):
    """Traduction déjà connue (aucun appel réseau), sinon None."""
    try:
        with open(_cache_path(ctx, text, src), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def translate(ctx, text, src="en"):
    """(texte français, None) ou (None, message d'erreur). Résultat mis en cache sur disque."""
    text = (text or "").strip()
    if not text:
        return "", None
    cfg = get_config(ctx.repo)
    provider = cfg["provider"]
    if provider not in ("google", "mymemory", "libretranslate"):
        return None, "traduction désactivée"
    src = (src or "en").split("-")[0].lower()
    key = hashlib.sha1(f"{src}\0{text}".encode()).hexdigest()
    path = os.path.join(ctx.settings.cache_dir, "translations", key)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except OSError:
        pass
    funcs = {"google": _google, "mymemory": _mymemory, "libretranslate": _libretranslate}
    chain = [provider]
    if cfg.get("fallback") and provider != "libretranslate":      # un service privé choisi ne bascule jamais vers l'extérieur
        chain += [p for p in ("google", "mymemory") if p != provider]
    result, last_err = "", "traduction indisponible"
    for name in chain:
        try:
            result = funcs[name](ctx.fast_session, cfg, text, src).strip()
        except TranslateError as e:
            last_err = str(e)
            continue
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            last_err = f"{LABELS[name]} injoignable ({type(e).__name__})"           # sans adresse ni clé
            if name in ("google", "mymemory"):
                _block(name, SHORT_BLOCK)       # service en panne : on ne le réessaie pas pour chaque carte
            continue
        if result:
            break
    if not result:
        return None, last_err
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(result)
    os.replace(path + ".tmp", path)
    return result, None


def reset_block():
    _blocked.clear()
