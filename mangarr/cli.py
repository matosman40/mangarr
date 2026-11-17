"""Ligne de commande : python -m mangarr [run|list|sources|add|remove|daemon] ..."""
import argparse
import json
import os
import sys
import time

from mangarr.core.config import load_settings
from mangarr.core.engine import Context, RunOptions, check_all
from mangarr.sources import SOURCES

COMMANDS = {"run", "list", "sources", "add", "remove", "daemon", "serve", "import"}


def parser():
    ap = argparse.ArgumentParser(prog="mangarr", description="Télécharge les nouveaux chapitres en CBZ.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="vérifier les mangas (commande par défaut)")
    run.add_argument("--only", nargs="+", metavar="NOM", help="ne traiter que ces mangas")
    run.add_argument("--max", type=int, help="nombre max de chapitres par manga")
    run.add_argument("--dry-run", action="store_true", help="n'écrit/télécharge rien")
    run.add_argument("--verbose", "-v", action="store_true")
    run.add_argument("--no-tomes", action="store_true", help="ne pas assembler les tomes")
    run.add_argument("--tomes-only", action="store_true", help="n'assembler que les tomes")
    run.add_argument("--keep-chapters", action="store_true", help="ne pas supprimer les chapitres assemblés")
    run.add_argument("--page-delay", type=float)
    run.add_argument("--chapter-delay", type=float)
    sub.add_parser("list", help="lister les mangas suivis")
    sub.add_parser("sources", help="lister les sources disponibles")
    add = sub.add_parser("add", help="ajouter un manga")
    add.add_argument("name")
    add.add_argument("--source", default="pattern")
    add.add_argument("--start", type=int, default=1, help="chapitre de départ")
    add.add_argument("--param", action="append", default=[], metavar="CLE=VALEUR",
                     help="paramètre de source (répétable), ex. slug=one-piece")
    add.add_argument("--disabled", action="store_true")
    rm = sub.add_parser("remove", help="supprimer un manga du suivi (les fichiers sont conservés)")
    rm.add_argument("name")
    sub.add_parser("daemon", help="planificateur seul (sans interface)")
    imp = sub.add_parser("import", help="importer la liste MANGAS de l'ancien script (simulation par défaut)")
    imp.add_argument("file")
    imp.add_argument("--apply", action="store_true", help="écrire réellement dans la base")
    imp.add_argument("--keep-folders", action="store_true", help="reprendre les dossiers folder/tomes_folder")
    imp.add_argument("--chapters-root", help="dossier des chapitres actuels : reprend .mangadl_state.json")
    imp.add_argument("--patreon-dir", help="dossier des exports Patreon à copier dans /config/patreon")
    sub.add_parser("serve", help="interface web + planificateur")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS | {"-h", "--help"}:
        argv.insert(0, "run")
    args = parser().parse_args(argv)
    settings = load_settings()
    if args.cmd == "run":
        if args.page_delay is not None:
            settings.page_delay = args.page_delay
        if args.chapter_delay is not None:
            settings.chapter_delay = args.chapter_delay
    ctx = Context(settings)

    if args.cmd == "sources":
        for k, cls in sorted(SOURCES.items()):
            doc = (cls.__doc__ or "").strip().splitlines()
            print(f"{k:<12} {doc[0] if doc else ''}")
        for e in ctx.plugin_errors:
            print(f"⚠️ {e}", file=sys.stderr)
        return 0
    if args.cmd == "list":
        for m in ctx.repo.list_mangas():
            st = ctx.repo.get_state(m["id"])
            print(f"{'✔' if m['enabled'] else '✘'} {m['name']:<28} [{m['source']}] "
                  f"dernier : {st.get('last_chapter') or '-'}")
        return 0
    if args.cmd == "add":
        params = {}
        for p in args.param:
            if "=" not in p:
                print(f"paramètre invalide : {p}", file=sys.stderr)
                return 2
            k, v = p.split("=", 1)
            try:
                params[k] = json.loads(v)
            except ValueError:
                params[k] = v
        if ctx.repo.get_manga(args.name):
            print(f"{args.name} existe déjà", file=sys.stderr)
            return 2
        if args.source not in SOURCES:
            print(f"source inconnue : {args.source}", file=sys.stderr)
            return 2
        ctx.repo.add_manga(args.name, args.source, params, args.start, enabled=not args.disabled)
        print(f"{args.name} ajouté")
        return 0
    if args.cmd == "remove":
        m = ctx.repo.get_manga(args.name)
        if not m:
            print("manga inconnu", file=sys.stderr)
            return 2
        ctx.repo.delete_manga(m["id"])
        print(f"{m['name']} retiré du suivi")
        return 0
    if args.cmd == "import":
        from mangarr.importer import import_mangas, parse_config
        try:
            with open(args.file, encoding="utf-8") as f:
                mangas = parse_config(f.read())
        except (OSError, ValueError) as e:
            print(f"Import impossible : {e}", file=sys.stderr)
            return 2
        for r in import_mangas(ctx, mangas, args.apply, args.keep_folders, args.chapters_root, args.patreon_dir):
            print(f"{r['action']:<14} {r['name']:<32} [{r['source']}] {r['note']}")
        if not args.apply:
            print("\nSimulation : relancez avec --apply pour écrire dans la base.")
        return 0
    if args.cmd == "serve":
        import uvicorn
        from mangarr.scheduler import Scheduler
        from mangarr.web.app import create_app
        sched = Scheduler(ctx)
        sched.start()
        if not os.environ.get("MANGARR_PASSWORD"):
            print("⚠️ MANGARR_PASSWORD non défini : l'interface est ouverte à tous. Ne l'exposez pas sur Internet.")
        uvicorn.run(create_app(ctx, sched), host=os.environ.get("MANGARR_HOST", "0.0.0.0"),
                    port=settings.port, log_level="warning")
        sched.stop()
        return 0
    if args.cmd == "daemon":
        from mangarr.scheduler import Scheduler
        s = Scheduler(ctx)
        s.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            s.stop()
        return 0

    opts = RunOptions(max=args.max, dry_run=args.dry_run, verbose=args.verbose, no_tomes=args.no_tomes,
                      tomes_only=args.tomes_only, keep_chapters=args.keep_chapters)
    if args.only:
        unknown = [n for n in args.only if not ctx.repo.get_manga(n)]
        if unknown:
            print(f"Manga inconnu : {', '.join(unknown)}", file=sys.stderr)
            return 2
    try:
        results = check_all(ctx, only=args.only, opts=opts)
    except KeyboardInterrupt:
        print("\nInterrompu. Les CBZ complets déjà créés sont conservés.")
        return 130
    print("\n──────── Résumé ────────")
    for r in results:
        note = {"ok": "", "missing": "(à jour)", "error": "⚠️ arrêt sur erreur"}[r.status]
        print(f"  {r.name:<28} {r.got} nouveau(x) {note}")
    return 1 if any(r.status == "error" for r in results) else 0
