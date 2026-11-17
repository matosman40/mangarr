"""Gestion : assistant d'ajout, édition, éditeur de sources, dépôt des exports Patreon."""
import json
import os
import re
import time
from urllib.parse import quote, urlparse

from fastapi import Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from mangarr import catalog
from mangarr.autoadd import needs_setup
from mangarr.core.util import clean_name
from mangarr.core.net import ChapterMissing, FetchError, fetch_image, sniff_image_ext
from mangarr.sources import (BUILTIN, OVERRIDES, SOURCES, HtmlSource, PatternSource, build_source, defaults_of,
                             instantiate, load_custom_sources, load_overrides, searchable_names)
from mangarr.sources import autosearch
from mangarr.sources.patreon import API as PATREON_API
from mangarr.sources.patreon import PatreonSource, inspect_export, post_id
from mangarr.tomes import mangadex
from mangarr.web.app_utils import fmt_ts

FIELD_LABELS = {
    "slug": ("Identifiant (slug) du manga sur le site", "one-piece"),
    "domain": ("Domaine du site", "exemple.fr"),
    "base_img_url": ("Adresse de base des images ({slug} et {domain} acceptés)", "https://site/mangas/{slug}"),
    "chapter_url": ("Modèle d'adresse du chapitre ({chap} = numéro)", "https://site/manga/mon-manga/{chap}"),
    "img_regex": ("Expression régulière des images (1 groupe)", ""),
    "img_filter": ("Filtre : l'adresse de l'image doit contenir (regex)", "/uploads/"),
    "page_start": ("Première page (0 ou 1)", "0"),
    "page_format": ("Format du numéro de page (02 ou d)", "02"),
    "search_url": ("Recherche : adresse avec {q} (texte) et {page}", "https://site/manga?title={q}&page={page}"),
    "result_regex": ("Recherche : expression des résultats (groupes nommés slug, title, cover)",
                     '<a href="/manga/(?P<slug>[^"]+)".*?data-src="(?P<cover>[^"]+)".*?alt="(?P<title>[^"]*)"'),
}
SEARCH_KEYS = ["search_url", "result_regex"]
SOURCE_NAME_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
MAX_UPLOAD = 5 * 1024 * 1024


def kind_of(cls):
    if issubclass(cls, PatreonSource):
        return "patreon"
    if issubclass(cls, HtmlSource):
        return "html"
    if issubclass(cls, PatternSource):
        return "pattern"
    return "plugin"


def keys_for_kind(kind):
    return {"html": ["chapter_url", "slug", "domain", "img_regex", "img_filter"],
            "pattern": ["base_img_url", "slug", "domain", "page_start", "page_format"]}.get(kind, [])


def source_keys(kind):
    """Champs de l'éditeur de sources : réglages du site + recherche."""
    return keys_for_kind(kind) + SEARCH_KEYS


def source_info(source_key):
    cls = SOURCES.get(source_key)
    kind = kind_of(cls) if cls else "plugin"
    return kind, keys_for_kind(kind), (cls.defaults if cls else {})


def posts_to_text(posts):
    if isinstance(posts, (list, tuple)):
        return "\n".join(f"{i + 1} {u}" for i, u in enumerate(posts))
    return "\n".join(f"{k} {v}" for k, v in sorted((posts or {}).items(), key=lambda kv: int(kv[0])))


def parse_posts(text):
    posts = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(https?://\S+)$", line)
        if not m:
            raise ValueError(f"ligne de post invalide : « {line[:60]} » (format : « 1 https://www.patreon.com/... »)")
        post_id(m.group(2))   # vérifie la présence de l'identifiant
        posts[m.group(1)] = m.group(2)
    return posts


def parse_params(form, source_key):
    kind, keys, _ = source_info(source_key)
    params = {}
    for k in keys:
        v = (form.get(f"p_{k}") or "").strip()
        if v:
            params[k] = int(v) if k == "page_start" and re.fullmatch(r"-?\d+", v) else v
    if kind == "patreon":
        params["posts"] = parse_posts(form.get("posts_text"))
    for k in ("mangadex_id", "mangadex_title"):
        if (form.get(k) or "").strip():
            params[k] = form[k].strip()
    extra = (form.get("extra") or "").strip()
    if extra:
        try:
            obj = json.loads(extra)
        except ValueError as e:
            raise ValueError(f"paramètres avancés : JSON invalide ({e})")
        if not isinstance(obj, dict):
            raise ValueError("paramètres avancés : un objet JSON { ... } est attendu")
        params.update(obj)
    return params


def values_from_form(form):
    keys = ("name", "source", "start_chap", "folder", "tomes_folder", "mangadex_id", "mangadex_title",
            "cover_url", "extra", "posts_text", "chap")
    v = {k: (form.get(k) or "").strip() for k in keys}
    v["tomes_enabled"] = form.get("tomes_enabled") is not None
    v["enabled"] = form.get("enabled") is not None
    v["p"] = {k[2:]: form[k] for k in form.keys() if k.startswith("p_")}
    return v


def values_from_manga(m):
    kind, keys, _ = source_info(m["source"])
    params = dict(m["params"])
    v = {"name": m["name"], "source": m["source"], "start_chap": m["start_chap"], "folder": m["folder"] or "",
         "tomes_folder": m["tomes_folder"] or "", "tomes_enabled": m["tomes_enabled"], "enabled": m["enabled"],
         "mangadex_id": params.pop("mangadex_id", ""), "mangadex_title": params.pop("mangadex_title", ""),
         "cover_url": "", "posts_text": posts_to_text(params.pop("posts", {})) if kind == "patreon" else "",
         "p": {k: params.pop(k) for k in keys if k in params}}
    v["extra"] = json.dumps(params, indent=2, ensure_ascii=False) if params else ""
    return v


def probe(ctx, src, chap):
    """Texte HTML du résultat d'un test : nombre d'images trouvées ou erreur explicite."""
    try:
        n = src.probe(ctx.session, chap)
        return True, f"{n} image(s) trouvée(s) pour le chapitre {chap}."
    except ChapterMissing as e:
        return False, f"Chapitre {chap} introuvable : {e}"
    except FetchError as e:
        return False, f"Erreur : {e}"
    except KeyError as e:
        return False, f"Paramètre manquant : {e}"
    except Exception as e:   # un test ne doit jamais renvoyer une erreur 500
        return False, f"Erreur inattendue : {e!r}"


def register(app, ctx, render):
    repo = ctx.repo

    def source_options():
        return sorted(SOURCES)

    def form_ctx(request, v, error=None, edit_id=None):
        kind, keys, defaults = source_info(v.get("source") or "pattern")
        return dict(v=v, error=error, edit_id=edit_id, sources=source_options(), kind=kind,
                    keys=keys, labels=FIELD_LABELS, defaults=defaults)

    # ------------------------------------------------------------------ ajout
    searchable = searchable_names

    def followed_slugs():
        return {(m["source"], str(m["params"].get("slug", ""))) for m in repo.list_mangas()}

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, source: str = "", slug: str = "", name: str = "", mdid: str = "",
                 mdtitle: str = "", cover: str = ""):
        found = searchable()
        src = source if source in SOURCES else (found[0] if found else "pattern")
        mdid = mdid.strip() if re.fullmatch(r"[0-9a-fA-F-]{36}", mdid.strip()) else ""
        title = (mdtitle or name).strip()
        v = {"source": src, "start_chap": 1, "tomes_enabled": True, "enabled": True,
             "p": {"slug": slug} if slug else {}, "name": clean_name(name or title),
             "mangadex_id": mdid, "mangadex_title": mdtitle.strip() if mdid else "",
             "cover_url": cover if mdid and cover.startswith("https://uploads.mangadex.org/") else ""}
        # titre connu : on lance la recherche sur les sites ; MangaDex n'est cherché que si l'association manque
        return render(request, "add.html", mdq="" if mdid else name, site_q=title if (title and not slug) else "",
                      md_linked=bool(mdid), md_title=mdtitle, browse_sources=found, **form_ctx(request, v))

    def run_search(name, q, page):
        src = instantiate(name, page_delay=0)
        try:
            return name, src.search(ctx.fast_session, q, page), None
        except NotImplementedError:
            return name, [], "pas de recherche pour cette source"
        except (FetchError, ChapterMissing) as e:
            return name, [], str(e)
        except Exception as e:   # un site qui change de structure ne doit pas casser la page
            return name, [], f"erreur inattendue : {e!r}"

    @app.get("/browse", response_class=HTMLResponse)
    async def browse(request: Request, source: str = "", q: str = "", page: int = 1, mdid: str = "",
                     mdtitle: str = "", cover: str = ""):
        from concurrent.futures import ThreadPoolExecutor
        names = [source] if source in SOURCES else searchable()
        page = max(1, min(page, 200))
        q = q.strip()

        def work():
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(names)))) as pool:
                return list(pool.map(lambda n: run_search(n, q, page), names))

        raw = await run_in_threadpool(work) if names else []
        mine = followed_slugs()
        groups = [{"source": n, "error": err,
                   "results": [{**r, "followed": (n, r["slug"]) in mine} for r in res]} for n, res, err in raw]
        more = any(len(g["results"]) >= 10 for g in groups)
        carry = ""
        if re.fullmatch(r"[0-9a-fA-F-]{36}", mdid):
            carry = "&amp;mdid=" + quote(mdid) + "&amp;mdtitle=" + quote(mdtitle) + "&amp;cover=" + quote(cover)
        return render(request, "_browse.html", groups=groups, q=q, page=page, source=source, more=more,
                      carry=carry, md_title=mdtitle if carry else "")

    @app.post("/sources/test-search", response_class=HTMLResponse)
    async def source_test_search(request: Request):
        form = await request.form()
        try:
            kind, d = parse_source_form(form)
            if not d.get("search_url"):
                raise ValueError("renseignez l'adresse de recherche")
            if d.get("result_regex"):
                re.compile(d["result_regex"])
            base = HtmlSource if kind == "html" else PatternSource
            src = base(dict(d), page_delay=0)
        except (ValueError, re.error) as e:
            return render(request, "_test_result.html", ok=False, msg=str(e))
        _, res, err = await run_in_threadpool(lambda: run_search_with(src, (form.get("t_q") or "").strip()))
        if err:
            return render(request, "_test_result.html", ok=False, msg=err)
        if not res:
            return render(request, "_test_result.html", ok=False,
                          msg="aucun résultat : vérifiez l'expression (groupes nommés slug, title, cover)")
        titles = ", ".join(r["title"] for r in res[:5])
        return render(request, "_test_result.html", ok=True, msg=f"{len(res)} résultat(s) : {titles}…")

    def run_search_with(src, q):
        try:
            return "", src.search(ctx.session, q, 1), None
        except (FetchError, ChapterMissing) as e:
            return "", [], str(e)
        except Exception as e:
            return "", [], f"erreur inattendue : {e!r}"

    @app.get("/add/search", response_class=HTMLResponse)
    async def add_search(request: Request, q: str = ""):
        results, error = [], None
        if q.strip():
            try:
                results, _ = await run_in_threadpool(mangadex.search, ctx.fast_session, q.strip(),
                                                     ctx.settings.mangadex_api)
            except Exception as e:
                error = f"Recherche MangaDex impossible : {e}"
        return render(request, "_search_results.html", results=results, error=error, q=q)

    @app.get("/add/fields", response_class=HTMLResponse)
    def add_fields(request: Request, source: str = "pattern"):
        return render(request, "_fields.html", **form_ctx(request, {"source": source, "p": {}}))

    @app.post("/add/test", response_class=HTMLResponse)
    async def add_test(request: Request):
        form = await request.form()
        v = values_from_form(form)
        try:
            params = parse_params(form, v["source"])
            src, merged = build_source(v["source"], params, ctx.settings)
            chap = int(v["chap"] or v["start_chap"] or 1)
        except (ValueError, KeyError) as e:
            return render(request, "_test_result.html", ok=False, msg=str(e))
        ok, msg = await run_in_threadpool(probe, ctx, src, chap)
        return render(request, "_test_result.html", ok=ok, msg=msg)

    async def save_manga(request, edit_id=None):
        form = await request.form()
        v = values_from_form(form)
        try:
            name = clean_name(v["name"]) if not edit_id else repo.get_manga(edit_id)["name"]
            if not name:
                raise ValueError("le nom est obligatoire")
            if v["source"] not in SOURCES:
                raise ValueError(f"source inconnue : {v['source']}")
            if not edit_id and repo.get_manga(name):
                raise ValueError(f"« {name} » est déjà suivi")
            params = parse_params(form, v["source"])
            start = int(v["start_chap"] or 1)
            if start < 0:
                raise ValueError("le chapitre de départ doit être positif")
        except ValueError as e:
            v["name"] = v["name"] or ""
            return render(request, "edit.html" if edit_id else "add.html", status_code=400,
                          **form_ctx(request, v, error=str(e), edit_id=edit_id))
        fields = dict(source=v["source"], params=params, start_chap=start, folder=v["folder"] or None,
                      tomes_folder=v["tomes_folder"] or None, tomes_enabled=v["tomes_enabled"],
                      enabled=v["enabled"] if edit_id else True)
        if edit_id:
            repo.update_manga(edit_id, **fields)
            mid = edit_id
        else:
            mid = repo.add_manga(name, **fields)
            await run_in_threadpool(store_cover, mid, v["cover_url"])
        return RedirectResponse(f"/manga/{mid}", status_code=303)

    def store_cover(mid, url):
        from mangarr.tomes.covers import store_md_cover
        store_md_cover(ctx, mid, url)

    @app.post("/add")
    async def add_submit(request: Request):
        return await save_manga(request)

    @app.get("/manga/{mid}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, mid: int):
        m = repo.get_manga(mid)
        if not m:
            return Response("Manga introuvable", status_code=404)
        v = values_from_manga(m)
        if needs_setup(m, repo.get_state(mid)):
            v["enabled"] = True       # le but de la modification : finir la configuration puis activer le suivi
        return render(request, "edit.html", **form_ctx(request, v, edit_id=mid))

    @app.post("/manga/{mid}/edit")
    async def edit_submit(request: Request, mid: int):
        if not repo.get_manga(mid):
            return Response("Manga introuvable", status_code=404)
        return await save_manga(request, mid)

    # ---------------------------------------------------------------- sources
    def source_rows():
        custom = {r["name"]: r for r in repo.list_custom_sources()}
        rows = []
        for name in sorted(SOURCES):
            cls = SOURCES[name]
            origin = "intégrée" if name in BUILTIN else ("formulaire" if name in custom else "plugin")
            doc = ((cls.__doc__ or "").strip().splitlines() or [""])[0]
            try:
                can_search = instantiate(name).can_search()
            except Exception:
                can_search = False
            rows.append({"name": name, "origin": origin, "kind": kind_of(cls), "doc": doc, "search": can_search,
                         "used": sum(1 for m in repo.list_mangas() if m["source"] == name)})
        return rows

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        return render(request, "sources.html", rows=source_rows(), errors=ctx.plugin_errors,
                      plugins_dir=ctx.settings.plugins_dir, catalog_status=catalog.status(ctx), crawl=catalog.STATE)

    @app.post("/catalog/refresh")
    async def catalog_refresh(source: str = Form("")):
        catalog.refresh_async(ctx, only=source if source in SOURCES else None)
        return RedirectResponse("/sources", status_code=303)

    def src_form_ctx(v, error=None, existing=False):
        kind = v.get("kind", "html")
        return dict(v=v, error=error, existing=existing, keys=source_keys(kind), labels=FIELD_LABELS)

    @app.get("/sources/new", response_class=HTMLResponse)
    def source_new(request: Request):
        return render(request, "source_form.html", **src_form_ctx({"kind": "html", "d": {}}))

    @app.get("/sources/fields", response_class=HTMLResponse)
    def source_fields(request: Request, kind: str = "html"):
        return render(request, "_srcfields.html", **src_form_ctx({"kind": kind, "d": {}}))

    @app.get("/sources/{name}/edit", response_class=HTMLResponse)
    def source_edit(request: Request, name: str):
        row = next((r for r in repo.list_custom_sources() if r["name"] == name), None)
        if not row:
            return Response("Source introuvable (seules les sources « formulaire » sont modifiables)",
                            status_code=404)
        v = {"name": name, "kind": row["kind"], "description": row["description"] or "", "d": row["defaults"]}
        return render(request, "source_form.html", **src_form_ctx(v, existing=True))

    def parse_source_form(form):
        kind = form.get("kind", "html")
        if kind not in ("html", "pattern"):
            raise ValueError("type de source invalide")
        d = {}
        for k in source_keys(kind):
            val = (form.get(f"d_{k}") or "").strip()
            if val:
                d[k] = int(val) if k == "page_start" and re.fullmatch(r"-?\d+", val) else val
        return kind, d

    @app.post("/sources/test", response_class=HTMLResponse)
    async def source_test(request: Request):
        form = await request.form()
        try:
            kind, d = parse_source_form(form)
            base = HtmlSource if kind == "html" else PatternSource
            params = dict(d)
            if (form.get("t_slug") or "").strip():
                params["slug"] = form["t_slug"].strip()
            src = base(params, page_delay=ctx.settings.page_delay)
            chap = int(form.get("chap") or 1)
        except (ValueError, KeyError) as e:
            return render(request, "_test_result.html", ok=False, msg=str(e))
        ok, msg = await run_in_threadpool(probe, ctx, src, chap)
        return render(request, "_test_result.html", ok=ok, msg=msg)

    @app.post("/sources")
    async def source_save(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip().lower()
        existing = form.get("existing") == "1"
        try:
            if not SOURCE_NAME_RE.match(name):
                raise ValueError("nom invalide : 2 à 32 caractères, minuscules, chiffres, - et _")
            customs = {r["name"] for r in repo.list_custom_sources()}
            if name in SOURCES and name not in customs:
                raise ValueError(f"« {name} » existe déjà (source intégrée ou plugin)")
            if name in customs and not existing:
                raise ValueError(f"« {name} » existe déjà")
            kind, d = parse_source_form(form)
            if kind == "html" and "chapter_url" not in d:
                raise ValueError("le modèle d'adresse du chapitre est obligatoire")
            if kind == "pattern" and "base_img_url" not in d:
                raise ValueError("indiquez l'adresse de base des images")
            if d.get("img_regex"):
                re.compile(d["img_regex"])
            if d.get("img_filter"):
                re.compile(d["img_filter"])
            if d.get("result_regex") and not d.get("search_url"):
                raise ValueError("une expression de résultats demande aussi l'adresse de recherche")
            if d.get("result_regex"):
                if "slug" not in re.compile(d["result_regex"]).groupindex:
                    raise ValueError("l'expression des résultats doit contenir un groupe nommé (?P<slug>...)")
        except (ValueError, re.error) as e:
            v = {"name": name, "kind": form.get("kind", "html"), "description": form.get("description", ""),
                 "d": {k[2:]: form[k] for k in form.keys() if k.startswith("d_")}}
            return render(request, "source_form.html", status_code=400,
                          **src_form_ctx(v, error=str(e), existing=existing))
        repo.save_custom_source(name, kind, d, (form.get("description") or "").strip())
        load_custom_sources(repo)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{name}/delete")
    def source_delete(request: Request, name: str):
        customs = {r["name"] for r in repo.list_custom_sources()}
        if name not in customs:
            return Response("Source introuvable", status_code=404)
        if any(m["source"] == name for m in repo.list_mangas()):
            return render(request, "sources.html", status_code=409, rows=source_rows(), errors=ctx.plugin_errors,
                          plugins_dir=ctx.settings.plugins_dir, catalog_status=catalog.status(ctx), crawl=catalog.STATE,
                          flash=f"« {name} » est utilisée par des mangas : modifiez-les d'abord.")
        repo.delete_custom_source(name)
        SOURCES.pop(name, None)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/reload")
    def sources_reload():
        ctx.reload_sources()
        return RedirectResponse("/sources", status_code=303)

    # ------------------------------------------- recherche d'une source : réglage, détection, test
    def search_values(name):
        d = defaults_of(name)
        return {"search_url": d.get("search_url", ""), "result_regex": d.get("result_regex", ""),
                "domain": d.get("domain", "")}

    def site_guess(name):
        d = defaults_of(name)
        if d.get("domain"):
            return "https://" + d["domain"]
        for key in ("chapter_url", "base_template", "base_img_url"):
            host = urlparse(d.get(key, "")).netloc
            if host and "{" not in host:
                return f"{urlparse(d[key]).scheme}://{host}"
        return ""

    def search_ctx(name, values=None, error=None):
        return dict(src_name=name, v=values or search_values(name), error=error, site=site_guess(name),
                    overridden=name in OVERRIDES)

    @app.get("/sources/{name}/search", response_class=HTMLResponse)
    def source_search_page(request: Request, name: str):
        if name not in SOURCES:
            return Response("Source introuvable", status_code=404)
        return render(request, "source_search.html", **search_ctx(name))

    def parse_search_form(form):
        v = {k: (form.get(k) or "").strip() for k in ("search_url", "result_regex", "domain")}
        if v["search_url"] and not v["search_url"].startswith(("http://", "https://")):
            raise ValueError("l'adresse de recherche doit commencer par http:// ou https://")
        if v["search_url"] and "{q}" not in v["search_url"]:
            raise ValueError("l'adresse de recherche doit contenir {q} (le texte cherché)")
        if v["result_regex"]:
            try:
                rx = re.compile(v["result_regex"])
            except re.error as e:
                raise ValueError(f"expression invalide : {e}")
            if "slug" not in rx.groupindex:
                raise ValueError("l'expression doit contenir un groupe nommé (?P<slug>...)")
        return v

    @app.post("/sources/{name}/search", response_class=HTMLResponse)
    async def source_search_save(request: Request, name: str):
        if name not in SOURCES:
            return Response("Source introuvable", status_code=404)
        form = await request.form()
        overrides = repo.get_setting("source_overrides", {})
        if form.get("reset"):
            overrides.pop(name, None)
        else:
            try:
                v = parse_search_form(form)
            except ValueError as e:
                return render(request, "source_search.html", status_code=400,
                              **search_ctx(name, {k: (form.get(k) or "") for k in ("search_url", "result_regex", "domain")}, str(e)))
            clean = {k: val for k, val in v.items() if val}
            if clean:
                overrides[name] = clean
            else:
                overrides.pop(name, None)
        repo.set_setting("source_overrides", overrides)
        load_overrides(repo)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{name}/search/test", response_class=HTMLResponse)
    async def source_search_test(request: Request, name: str):
        if name not in SOURCES:
            return Response("Source introuvable", status_code=404)
        form = await request.form()
        try:
            v = parse_search_form(form)
            if not v["search_url"]:
                raise ValueError("renseignez l'adresse de recherche")
            src = SOURCES[name]({**SOURCES[name].defaults, **{k: val for k, val in v.items() if val}}, page_delay=0)
        except ValueError as e:
            return render(request, "_test_result.html", ok=False, msg=str(e))
        _, res, err = await run_in_threadpool(lambda: run_search_with(src, (form.get("t_q") or "").strip()))
        if err:
            return render(request, "_test_result.html", ok=False, msg=err)
        if not res:
            return render(request, "_test_result.html", ok=False,
                          msg="aucun résultat : essayez « Détecter automatiquement » ou un autre texte")
        return render(request, "_test_result.html", ok=True,
                      msg=f"{len(res)} résultat(s) : " + ", ".join(r["title"] for r in res[:5]) + "…")

    @app.post("/sources/{name}/search/detect", response_class=HTMLResponse)
    async def source_search_detect(request: Request, name: str):
        if name not in SOURCES:
            return Response("Source introuvable", status_code=404)
        form = await request.form()
        site = (form.get("site") or "").strip()
        try:
            url, hits = await run_in_threadpool(autosearch.detect, ctx.fast_session, site)
        except ValueError as e:
            return render(request, "_detect_result.html", ok=False, msg=str(e), url="")
        if not url:
            return render(request, "_detect_result.html", ok=False, url="",
                          msg="aucune recherche fiable détectée sur ce site (il peut être protégé ou construit en JavaScript) : "
                              "saisissez l'adresse de recherche à la main")
        titles = ", ".join(h["title"] for h in hits[:4])
        return render(request, "_detect_result.html", ok=True, url=url,
                      msg=f"recherche détectée : {url} ({len(hits)} résultat(s) d'essai : {titles}…). Vérifiez puis enregistrez.")

    # ---------------------------------------------------------------- Patreon
    def patreon_rows():
        out = []
        for m in repo.list_mangas():
            if m["source"] != "patreon":
                continue
            try:
                src, params = build_source("patreon", dict(m["params"]), ctx.settings)
            except ValueError:
                continue
            folder = m.get("folder") or os.path.join(ctx.settings.library_dir, m["name"])
            posts = []
            for chap in src.explicit_chapters(params):
                url = src.post_url(params, chap)
                try:
                    pid = post_id(url)
                except FetchError:
                    posts.append({"chap": chap, "status": "badurl", "pid": "?", "have": False})
                    continue
                have = os.path.exists(os.path.join(folder, src.file_pattern.format(name=m["name"], chap=chap)))
                status, n, exp = inspect_export(src.export_path(url))
                posts.append({"chap": chap, "pid": pid, "have": have, "status": status, "images": n,
                              "exp": fmt_ts(exp) if exp else None, "api": PATREON_API.format(id=pid)})
            out.append({"manga": m, "posts": posts,
                        "todo": sum(1 for p in posts if not p["have"] and p["status"] in ("missing", "expired", "invalid"))})
        return out

    @app.get("/patreon", response_class=HTMLResponse)
    def patreon_page(request: Request):
        return render(request, "patreon.html", series=patreon_rows(), now=time.time())

    @app.post("/patreon/upload")
    async def patreon_upload(files: list[UploadFile]):
        known = set()
        for m in repo.list_mangas():
            if m["source"] == "patreon":
                for u in (m["params"].get("posts") or {}).values() if isinstance(m["params"].get("posts"), dict) \
                        else (m["params"].get("posts") or []):
                    try:
                        known.add(post_id(u))
                    except FetchError:
                        pass
        results = []
        for f in files:
            res = {"file": os.path.basename(f.filename or "?"), "ok": False}
            try:
                raw = await f.read(MAX_UPLOAD + 1)
                if len(raw) > MAX_UPLOAD:
                    raise ValueError("fichier trop gros (5 Mo max)")
                data = json.loads(raw.decode("utf-8-sig"))
                pid = str((data.get("data") or {}).get("id", "")) if isinstance(data, dict) else ""
                if not re.fullmatch(r"\d{6,}", pid):
                    raise ValueError("ce n'est pas l'export d'API d'un post Patreon (id du post introuvable)")
                from mangarr.sources.patreon import media_candidates
                if not media_candidates(data):
                    raise ValueError("aucune image dans cet export (post verrouillé pour votre compte ?)")
                os.makedirs(ctx.settings.patreon_dir, exist_ok=True)
                dest = os.path.join(ctx.settings.patreon_dir, f"{pid}.json")   # nom imposé : pas de traversée
                with open(dest + ".tmp", "wb") as out:
                    out.write(raw)
                os.replace(dest + ".tmp", dest)
                res.update(ok=True, id=pid, message="enregistré" + ("" if pid in known else
                           " (attention : ce post n'est dans aucune série configurée)"))
            except (ValueError, UnicodeDecodeError) as e:
                res["message"] = str(e) if isinstance(e, ValueError) else "fichier non lisible (UTF-8 attendu)"
            results.append(res)
        return JSONResponse(results)
