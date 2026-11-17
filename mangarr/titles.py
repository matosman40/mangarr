"""Comparaison de titres (sans accents de ponctuation, casse, séparateurs)."""
import re
from difflib import SequenceMatcher


def norm(title):
    return re.sub(r"[\W_]+", "", (title or "").casefold())


def score(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()
