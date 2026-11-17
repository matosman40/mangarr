"""Import de la configuration de l'ancien script (liste MANGAS) sans jamais l'exécuter (module ast)."""
import ast
import json
import os
import shutil

from mangarr.core.util import clean_name

SKIP_KEYS = {"name", "source", "start_chap", "enabled", "folder", "tomes_folder", "tomes", "cookie_env"}


def parse_config(text):
    """Extrait la liste littérale MANGAS. Lève ValueError si absente ou non littérale."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise ValueError(f"fichier Python invalide : {e}")
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(t, ast.Name) and t.id == "MANGAS" for t in targets) and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                raise ValueError("MANGAS contient autre chose que des valeurs littérales (texte, nombres, listes)")
            if not isinstance(value, list) or not all(isinstance(m, dict) for m in value):
                raise ValueError("MANGAS doit être une liste de dictionnaires")
            return value
    raise ValueError("liste MANGAS introuvable dans ce fichier")


def convert(entry, keep_folders=False):
    """Une entrée de l'ancien script -> arguments pour Repo.add_manga."""
    name = clean_name(str(entry.get("name", "")))
    if not name:
        raise ValueError("entrée sans nom")
    params = {k: v for k, v in entry.items() if k not in SKIP_KEYS}
    posts = params.get("posts")
    if isinstance(posts, (list, tuple)):
        start = int(entry.get("start_chap", 1))
        params["posts"] = {str(start + i): u for i, u in enumerate(posts)}
    elif isinstance(posts, dict):
        params["posts"] = {str(k): v for k, v in posts.items()}
    kw = dict(name=name, source=entry.get("source", "pattern"), params=params,
              start_chap=int(entry.get("start_chap", 1)), enabled=bool(entry.get("enabled", True)),
              tomes_enabled=bool(entry.get("tomes", True)))
    if keep_folders:
        kw["folder"] = entry.get("folder")
        kw["tomes_folder"] = entry.get("tomes_folder")
    return kw


def import_mangas(ctx, mangas, apply=False, keep_folders=False, chapters_root=None, patreon_dir=None):
    """Retourne une liste de lignes {name, source, action, note}. Sans apply : simulation."""
    rows = []
    for entry in mangas:
        try:
            kw = convert(entry, keep_folders)
        except (ValueError, TypeError) as e:
            rows.append({"name": str(entry.get("name", "?")), "source": entry.get("source", ""),
                         "action": "ignoré", "note": str(e)})
            continue
        row = {"name": kw["name"], "source": kw["source"], "action": "", "note": ""}
        if ctx.repo.get_manga(kw["name"]):
            row.update(action="existe déjà", note="conservé tel quel")
        else:
            row["action"] = "ajouté" if apply else "serait ajouté"
            if apply:
                mid = ctx.repo.add_manga(**kw)
                if chapters_root:
                    st = os.path.join(chapters_root, kw["name"], ".mangadl_state.json")
                    try:
                        with open(st) as f:
                            last = json.load(f).get("last_chapter")
                        if isinstance(last, int):
                            ctx.repo.save_last_chapter(mid, last)
                            row["note"] = f"dernier chapitre repris : {last}"
                    except (OSError, ValueError):
                        pass
        rows.append(row)
    if apply and patreon_dir and os.path.isdir(patreon_dir):
        os.makedirs(ctx.settings.patreon_dir, exist_ok=True)
        for fn in os.listdir(patreon_dir):
            dest = os.path.join(ctx.settings.patreon_dir, fn)
            if fn.endswith(".json") and os.path.splitext(fn)[0].isdigit() and not os.path.exists(dest):
                shutil.copyfile(os.path.join(patreon_dir, fn), dest)
    return rows
