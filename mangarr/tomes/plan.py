"""Règles d'assemblage des tomes : planification, écriture sûre, vérification."""
import os
import re
from zipfile import ZIP_STORED, ZipFile


def comicinfo_xml(series, number, pages, volume=None):
    vol = f"  <Volume>{volume}</Volume>\n" if volume is not None else ""
    title = series.replace("_", " ").replace("&", "&amp;").replace("<", "&lt;")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>\n'
            f"  <Series>{title}</Series>\n{vol}  <Number>{number}</Number>\n"
            f"  <PageCount>{pages}</PageCount>\n  <Manga>YesAndRightToLeft</Manga>\n</ComicInfo>\n")


def chapter_numbers(folder, name):
    pattern = re.compile(rf"^{re.escape(name)}_(\d+)\.cbz$")
    if not os.path.isdir(folder):
        return set()
    return {int(m.group(1)) for f in os.listdir(folder) if (m := pattern.match(f))}


def plan_tomes(volumes, local, strict=True):
    """Retourne (prêts, incomplets) : prêts = [(volume, chapitres)], incomplets = [(volume, raison)].

    strict : un tome n'est assemblé que si ses chapitres se raccordent à ceux des tomes voisins
    (sinon MangaDex ne rattache qu'une partie des chapitres au tome : tome incomplet).
    Les chapitres décimaux sont déjà exclus des données en amont.
    """
    ready, partial = [], []
    order = [v for v in sorted(volumes) if volumes[v]]
    for vol in sorted(volumes):
        chs = volumes[vol]
        if not chs or not (set(chs) & local):
            continue   # aucun chapitre chez l'utilisateur : ignoré
        gaps = sorted(set(range(chs[0], chs[-1] + 1)) - set(chs))
        i = order.index(vol)
        prev_last = volumes[order[i - 1]][-1] if i > 0 else None
        next_first = volumes[order[i + 1]][0] if i + 1 < len(order) else None
        if gaps:
            partial.append((vol, f"données MangaDex incomplètes (trous : {gaps[:5]})"))
        elif strict and prev_last is not None and chs[0] > prev_last + 1:
            partial.append((vol, f"chapitres {prev_last + 1}-{chs[0] - 1} non rattachés à un tome par MangaDex"))
        elif strict and next_first is not None and next_first > chs[-1] + 1:
            partial.append((vol, f"chapitres {chs[-1] + 1}-{next_first - 1} non rattachés à un tome par MangaDex"))
        elif set(chs) - local:
            miss = sorted(set(chs) - local)
            partial.append((vol, f"chapitres manquants : {miss[:6]}{'…' if len(miss) > 6 else ''}"))
        else:
            ready.append((vol, chs))
    return ready, partial


def _rm(path):
    if os.path.exists(path):
        os.remove(path)


def _images(z):
    return [n for n in z.namelist() if n.lower() != "comicinfo.xml"]


def count_images(path):
    with ZipFile(path) as z:
        return len(_images(z))


def build_tome(name, folder, vol, chs, out_dir):
    """Écrit le CBZ en .part, vérifie (testzip + pages), puis os.replace. Retourne (chemin, pages)."""
    final = os.path.join(out_dir, f"{name}_T{vol:02}.cbz")
    tmp = final + ".part"
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    try:
        with ZipFile(tmp, "w", ZIP_STORED) as zf:
            for c in chs:
                with ZipFile(os.path.join(folder, f"{name}_{c}.cbz")) as src:
                    for entry in sorted(_images(src)):
                        n += 1
                        zf.writestr(f"{n:05}{os.path.splitext(entry)[1]}", src.read(entry))
            zf.writestr("ComicInfo.xml", comicinfo_xml(name, chs[0], n, volume=vol))
        if not tome_is_valid(name, folder, tmp, chs):
            raise ValueError("vérification du tome échouée (archive illisible ou pages manquantes)")
    except Exception:
        _rm(tmp)
        raise
    os.replace(tmp, final)
    return final, n


def tome_is_valid(name, folder, tome_path, chs):
    """Le tome est lisible et contient exactement toutes les pages des chapitres à supprimer."""
    try:
        with ZipFile(tome_path) as z:
            if z.testzip() is not None:
                return False
        expected = sum(count_images(os.path.join(folder, f"{name}_{c}.cbz")) for c in chs)
        return count_images(tome_path) == expected
    except (OSError, ValueError, KeyError):
        return False
