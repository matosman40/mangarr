import os
import re

NAME_RE = re.compile(r"[^\w.\- ]", re.U)


def clean_name(raw):
    """Nom sûr pour un dossier/fichier : pas de séparateur de chemin, espaces -> _."""
    return NAME_RE.sub("", raw or "").strip().replace(" ", "_")[:80]


def permission_hint(path):
    """Explication d'une erreur « permission refusée » : qui exécute Mangarr, à qui appartient le dossier."""
    try:
        uid, gid = os.getuid(), os.getgid()
    except AttributeError:          # Windows : pas de notion d'UID
        return ""
    p = os.path.abspath(path)
    while p and not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    try:
        st = os.stat(p)
    except OSError:
        return ""
    return (f"Mangarr s'exécute avec l'utilisateur {uid}:{gid} ; « {p} » appartient à {st.st_uid}:{st.st_gid} "
            f"(droits {oct(st.st_mode & 0o777)}). Donnez l'accès en écriture à {uid}:{gid} (chown/chmod), "
            f"ou réglez l'utilisateur du conteneur sur {st.st_uid}:{st.st_gid}.")
