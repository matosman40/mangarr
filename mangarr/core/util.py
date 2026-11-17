import re

NAME_RE = re.compile(r"[^\w.\- ]", re.U)


def clean_name(raw):
    """Nom sûr pour un dossier/fichier : pas de séparateur de chemin, espaces -> _."""
    return NAME_RE.sub("", raw or "").strip().replace(" ", "_")[:80]
