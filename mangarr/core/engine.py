"""Moteur : téléchargement des chapitres, assemblage des tomes, vérification de tous les mangas."""
import os
import threading
import time
from dataclasses import dataclass
from zipfile import ZIP_STORED, ZipFile

from mangarr import sources as src_registry
from mangarr.core.config import Settings
from mangarr.core.db import Database, Repo
from mangarr.core.net import ChapterMissing, FetchError, make_session, sniff_image_ext
from mangarr.tomes import covers, mangadex
from mangarr.tomes.plan import (_rm, build_tome, chapter_numbers, comicinfo_xml, plan_tomes,
                                tome_is_valid)

RUN_LOCK = threading.Lock()   # une seule vérification à la fois (planificateur, interface, CLI)


@dataclass
class RunOptions:
    max: int | None = None
    dry_run: bool = False
    verbose: bool = False
    no_tomes: bool = False
    tomes_only: bool = False
    keep_chapters: bool = False


@dataclass
class Result:
    name: str
    got: int = 0
    status: str = "ok"      # ok | missing | error
    message: str = ""


class Context:
    """Regroupe réglages, base, session HTTP et journalisation."""

    def __init__(self, settings: Settings, db: Database | None = None, session=None, echo=print):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.repo = Repo(self.db)
        self.session = session or make_session()
        self.fast_session = session or make_session(retries=1)   # pages interactives : pas d'attente interminable
        self.echo = echo or (lambda *a, **k: None)
        self.plugin_errors = []
        self.reload_sources()

    def reload_sources(self):
        self.plugin_errors = src_registry.load_plugins(self.settings.plugins_dir, warn=lambda m: self.log("warn", m))
        src_registry.load_custom_sources(self.repo)

    def log(self, level, msg, manga_id=None):
        self.echo(msg)
        self.repo.add_log(level, msg, manga_id)


# --------------------------------------------------------------------------- #
def manga_folder(ctx, manga, source):
    if manga.get("folder"):
        return manga["folder"]
    base = ctx.settings.library_dir if source.in_library else ctx.settings.chapters_dir
    return os.path.join(base, manga["name"])


def tomes_folder(ctx, manga):
    return manga.get("tomes_folder") or os.path.join(ctx.settings.library_dir, manga["name"])


def chapter_path(source, name, folder, chap):
    return os.path.join(folder, source.file_pattern.format(name=name, chap=chap))


def download_chapter(ctx, source, manga, params, folder, chap, opts):
    """Retourne "ok", "missing" ou "error". Jamais de CBZ partiel."""
    name, mid = manga["name"], manga["id"]
    ctx.log("info", f"📥 {name} - chapitre {chap}", mid)
    final = chapter_path(source, name, folder, chap)
    tmp = final + ".part"
    count, last = 0, None
    try:
        with ZipFile(tmp, "w", ZIP_STORED) as zf:
            for data in source.iter_images(ctx.session, params, chap):
                if source.stop_on_duplicate and last is not None and data == last:
                    break
                ext = sniff_image_ext(data)
                if ext is None:
                    raise FetchError(f"données reçues qui ne sont pas une image (page {count + 1})")
                zf.writestr(f"{count + 1:03}{ext}", data)
                last, count = data, count + 1
                if opts.verbose:
                    ctx.echo(f"    page {count} ok ({len(data) // 1024} Ko)")
                if count >= ctx.settings.max_pages:
                    ctx.log("warn", f"Limite de {ctx.settings.max_pages} pages atteinte ({name} {chap})", mid)
                    break
            if count == 0:
                raise ChapterMissing(f"aucune image pour le chapitre {chap}")
            zf.writestr("ComicInfo.xml", comicinfo_xml(name, chap, count))
    except ChapterMissing as e:
        _rm(tmp)
        ctx.log("info", f"Chapitre {chap} introuvable (pas encore publié ?) : {e}", mid)
        return "missing", str(e)
    except Exception as e:
        _rm(tmp)
        ctx.log("error", f"❌ {name} chapitre {chap} abandonné : {e}", mid)
        return "error", str(e)
    os.replace(tmp, final)
    ctx.repo.add_history(mid, "chapter", chap, count, final)
    ctx.log("info", f"✅ {name} chapitre {chap} : {count} pages", mid)
    return "ok", ""


def handle_tomes(ctx, manga, folder, params, opts):
    name, mid = manga["name"], manga["id"]
    out_dir = tomes_folder(ctx, manga)
    keep = opts.keep_chapters or params.get("keep_chapters", False)
    try:
        vols, title, md_id = mangadex.volumes(ctx.session, ctx.settings.cache_dir, mid, name, params)
    except FetchError as e:
        ctx.log("warn", f"⚠️ {name} : tomes ignorés ({e})", mid)
        return
    local = chapter_numbers(folder, name)
    ready, partial = plan_tomes(vols, local, params.get("tome_strict", True))
    ctx.log("info", f"📚 {name} - tomes (MangaDex : « {title} », {len(vols)} volumes) -> {out_dir}", mid)
    if local and not opts.dry_run:
        ctx.repo.save_last_chapter(mid, max(local))   # avant toute suppression
    for vol, chs in ready:
        final = os.path.join(out_dir, f"{name}_T{vol:02}.cbz")
        rng = f"chapitres {chs[0]}-{chs[-1]}"
        if opts.dry_run:
            ctx.echo(f"    [dry-run] tome {vol} ({rng}) {'existe déjà' if os.path.exists(final) else 'serait créé'}")
            continue
        if not os.path.exists(final):
            try:
                _, pages = build_tome(name, folder, vol, chs, out_dir)
            except Exception as e:
                ctx.log("error", f"❌ {name} tome {vol} : {e}", mid)
                continue
            ctx.repo.add_history(mid, "tome", vol, pages, final)
            ctx.log("info", f"✅ {name} tome {vol} : {rng}, {pages} pages", mid)
        if keep:
            continue
        # nettoyage : uniquement si le tome contient exactement toutes les pages
        if tome_is_valid(name, folder, final, chs):
            for c in chs:
                _rm(os.path.join(folder, f"{name}_{c}.cbz"))
            ctx.log("info", f"🧹 {name} : {len(chs)} chapitres supprimés ({rng})", mid)
        else:
            ctx.log("warn", f"⚠️ {name} tome {vol} non conforme : chapitres CONSERVÉS", mid)
    for vol, why in partial:
        ctx.echo(f"    ⏳ tome {vol} pas encore assemblable : {why}")
    if not opts.dry_run:
        try:
            covers.ensure_cover(ctx, manga, folder, out_dir, md_id, params.get("mangadex_api", ctx.settings.mangadex_api))
        except Exception as e:   # la jaquette ne doit jamais faire échouer la vérification
            ctx.log("warn", f"jaquette : {e}", mid)


def check_manga(ctx, manga, opts=None) -> Result:
    opts = opts or RunOptions()
    name, mid = manga["name"], manga["id"]
    try:
        source, params = src_registry.build_source(manga["source"], dict(manga["params"]), ctx.settings)
    except ValueError as e:
        ctx.log("error", f"❌ {name} : {e}", mid)
        ctx.repo.set_status(mid, "error", str(e))
        return Result(name, 0, "error", str(e))
    folder = manga_folder(ctx, manga, source)
    if not opts.dry_run:
        os.makedirs(folder, exist_ok=True)
    got, status, message = 0, "ok", ""
    explicit = source.explicit_chapters(params)
    if explicit is not None:   # liste de numéros connue d'avance (Patreon) : chaque post est indépendant
        todo = [c for c in explicit if not os.path.exists(chapter_path(source, name, folder, c))]
        ctx.log("info", f"🟣 {name} [{source.name}] - {len(explicit)} post(s), {len(todo)} à récupérer", mid)
        for c in ([] if opts.tomes_only else todo[:opts.max]):
            if opts.dry_run:
                ctx.echo(f"    [dry-run] récupérerait le tome {c}")
                continue
            st, msg = download_chapter(ctx, source, manga, params, folder, c, opts)
            if st == "ok":
                got += 1
            else:
                status, message = "error", msg
            time.sleep(ctx.settings.chapter_delay)
    else:
        state = ctx.repo.get_state(mid)
        local = chapter_numbers(folder, name)
        cands = list(local) + ([state["last_chapter"]] if state.get("last_chapter") else [])
        last = max(cands) if cands else None
        chap = last + 1 if last else manga["start_chap"]
        ctx.log("info", f"{'🟢' if last else '🟡'} {name} [{source.name}] - "
                        f"{'dernier chapitre ' + str(last) if last else 'premier téléchargement'}, reprise au {chap}", mid)
        while not opts.tomes_only and (opts.max is None or got < opts.max):
            if opts.dry_run:
                ctx.echo(f"    [dry-run] téléchargerait le chapitre {chap}")
                break
            st, msg = download_chapter(ctx, source, manga, params, folder, chap, opts)
            if st != "ok":
                status, message = st, msg
                break      # numérotés : on s'arrête au premier échec (pas de trou)
            ctx.repo.save_last_chapter(mid, chap)
            got, chap = got + 1, chap + 1
            time.sleep(ctx.settings.chapter_delay)
        if (opts.tomes_only or not opts.no_tomes) and source.supports_tomes and manga["tomes_enabled"] \
                and os.path.isdir(folder):
            handle_tomes(ctx, manga, folder, params, opts)
    if not opts.dry_run:
        ctx.repo.set_status(mid, status, message)
    return Result(name, got, status, message)


def check_all(ctx, only=None, opts=None) -> list[Result]:
    """Vérifie les mangas actifs (ou ceux de `only`). Une erreur sur un manga n'arrête pas les autres."""
    opts = opts or RunOptions()
    if only:
        mangas = [m for n in only if (m := ctx.repo.get_manga(n))]
    else:
        mangas = ctx.repo.list_mangas(only_enabled=True)
    started = time.time()
    with RUN_LOCK:
        results = []
        for m in mangas:
            try:
                results.append(check_manga(ctx, m, opts))
            except Exception as e:   # filet de sécurité
                ctx.log("error", f"❌ {m['name']} : erreur inattendue ({e!r})", m["id"])
                results.append(Result(m["name"], 0, "error", repr(e)))
        ctx.repo.prune_logs()
    if not opts.dry_run and results:
        try:   # notifications / rafraîchissement : jamais bloquants pour la vérification
            from mangarr import notify
            notify.after_run(ctx, started, results)
        except Exception as e:
            ctx.repo.add_log("warn", f"notifications : {e!r}")
    return results
