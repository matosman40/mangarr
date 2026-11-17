import os
import threading
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mangarr import catalog
from mangarr.sources import SOURCES
from mangarr.tomes import covers
from mangarr.web.app import create_app
from tests.test_browse import CARD
from tests.test_discover import MID, entry

REAL_ENSURE = covers.ensure_async          # version réelle (les tests la remplacent par un enregistreur)


def cards(server, items):
    return "".join(CARD.format(base=server.url, slug=s, title=t) for s, t in items).encode()


def paged_site(server, pages, need_query=False):
    """Faux site : /manga?title=&page=N ; chaque page = une liste de (slug, titre)."""
    def route(path):
        qs = parse_qs(urlparse(path).query)
        page, q = int(qs.get("page", ["1"])[0]), qs.get("title", [""])[0]
        if need_query:
            return 200, "text/html", cards(server, [(f"x-{q}", f"Titre {q}")] if q and page == 1 else [])
        return 200, "text/html", cards(server, pages[page - 1] if page <= len(pages) else [])
    server.routes["/manga"] = route


@pytest.fixture
def site(server, monkeypatch):
    monkeypatch.setattr(SOURCES["lelscanfr"], "defaults", {
        **SOURCES["lelscanfr"].defaults, "search_url": server.url + "/manga?title={q}&page={page}"})
    return server


@pytest.fixture
def client(ctx, site):
    ctx.settings.mangadex_api = site.url + "/md"
    site.add_json("/md/manga/tag", {"data": []})
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def md_hits(server):
    return [parse_qs(urlparse(h).query) for h in server.hits if h.startswith("/md/manga?")]


# ------------------------------------------------------------------ lecture des catalogues
def test_refresh_reads_every_page_and_indexes_titles(ctx, site):
    paged_site(site, [[("one-piece", "One Piece"), ("bleach", "Bleach")], [("naruto", "Naruto")]])
    assert catalog.refresh(ctx) == 1
    assert catalog.total(ctx) == 3
    st = {s["source"]: s for s in catalog.status(ctx)}["lelscanfr"]
    assert st["count"] == 3 and st["error"] is None and st["refreshed"]
    assert catalog.match(ctx, ["Autre", "ONE-piece"])[:2] == ("lelscanfr", "one-piece")      # insensible à la casse/ponctuation
    assert catalog.match(ctx, ["Inconnu"]) is None


def test_failed_refresh_keeps_previous_catalog(ctx, site):
    paged_site(site, [[("one-piece", "One Piece")]])
    catalog.refresh(ctx)
    site.routes["/manga"] = (403, "text/html", b"blocked")
    catalog.refresh(ctx)
    st = {s["source"]: s for s in catalog.status(ctx)}["lelscanfr"]
    assert catalog.total(ctx) == 1 and st["error"] and "403" in st["error"]


def test_site_that_needs_a_query_is_swept_letter_by_letter(ctx, site):
    paged_site(site, [], need_query=True)
    catalog.refresh(ctx)
    assert catalog.total(ctx) == 36 and catalog.match(ctx, ["Titre a"])


def test_stale_only_skips_recent_catalogs(ctx, site):
    paged_site(site, [[("a", "A")]])
    catalog.refresh(ctx)
    n = len(site.hits)
    assert catalog.refresh(ctx, stale_only=True) == 0 and len(site.hits) == n


# ------------------------------------------------------------------ Découvrir : seulement les mangas des sources
def md_page(server, titles_by_offset=None, total=3, items=None):
    def route(path):
        qs = parse_qs(urlparse(path).query)
        off = int(qs.get("offset", ["0"])[0])
        data = items if items is not None else titles_by_offset(off)
        return 200, "application/json", __import__("json").dumps({"data": data, "total": total}).encode()
    server.routes["/md/manga"] = route


def seed(ctx, titles, source="lelscanfr"):
    catalog.store(ctx, source, [{"slug": t.lower().replace(" ", "-"), "title": t, "cover": None} for t in titles])


def test_discover_lists_only_mangas_available_on_sources(client, ctx, site):
    bleach = entry(3, "Bleach (JP)")
    bleach["attributes"]["altTitles"] = [{"en": "Bleach"}]
    md_page(site, items=[entry(1, "One Piece"), entry(2, "Naruto"), bleach], total=3)
    seed(ctx, ["One Piece", "Bleach"])
    ctx.repo.set_setting("discover_only_sources", True)
    t = client.get("/discover/results").text
    assert "One Piece" in t and "Bleach (JP)" in t and "Naruto" not in t and "disponibles sur vos sources" in t
    assert 'class="badge">lelscanfr' in t
    assert "Naruto" in client.get("/discover/results?all=1").text                 # case « afficher aussi les absents »
    ctx.repo.set_setting("discover_only_sources", False)
    assert "Naruto" in client.get("/discover/results").text                       # réglage global désactivé


def test_discover_scans_several_batches_until_enough(client, ctx, site):
    def page(off):
        return [entry(off + i + 1, f"Titre {off + i}") for i in range(100)]
    md_page(site, titles_by_offset=page, total=1000)
    seed(ctx, [f"Titre {n}" for n in range(0, 1000, 10)])                           # 1 titre sur 10 existe chez vous
    ctx.repo.set_setting("discover_only_sources", True)
    t = client.get("/discover/results").text
    q = md_hits(site)
    assert [h["offset"] for h in q] == [["0"], ["100"], ["200"]] and all(h["limit"] == ["100"] for h in q)
    assert t.count('class="poster"') == 30 and "offset=300" in t
    assert 'hx-trigger="revealed, click"' in t                                      # chargement automatique au défilement


def test_unfiltered_page_is_larger(client, ctx, site):
    md_page(site, items=[entry(1, "One Piece")], total=1)
    client.get("/discover/results")
    assert md_hits(site)[-1]["limit"] == ["48"]


def test_discover_waits_while_catalogs_are_read(client, ctx, site, no_background_work):
    ctx.repo.set_setting("discover_only_sources", True)
    t = client.get("/discover/results?q=x").text
    assert "Lecture des catalogues" in t and 'hx-trigger="every 3s"' in t and "q=x" in t
    assert no_background_work["catalog"]                                            # la lecture a été demandée
    md_page(site, items=[entry(1, "One Piece")], total=1)
    seed(ctx, ["One Piece"])
    assert "One Piece" in client.get("/discover/results?q=x").text                  # catalogue prêt : vrais résultats


def test_discover_explains_when_catalogs_are_empty_or_missing(client, ctx, site, monkeypatch):
    ctx.repo.set_setting("discover_only_sources", True)
    paged_site(site, [])
    catalog.refresh(ctx)                                                            # lu mais vide
    t = client.get("/discover/results").text
    assert "Aucun titre n'a pu être lu" in t and "every 3s" not in t
    monkeypatch.setattr("mangarr.web.discover.searchable_names", lambda: [])
    monkeypatch.setattr("mangarr.catalog.searchable_names", lambda: [])
    assert "Aucune source avec recherche" in client.get("/discover/results").text


# ------------------------------------------------------------------ ajout et nouvelle recherche
def test_follow_uses_the_catalog_instead_of_live_search(client, ctx, site):
    site.add_json(f"/md/manga/{MID}", {"data": entry(1, "One Piece")})
    seed(ctx, ["One Piece"])
    res = client.post("/discover/follow", data={"id": MID}, headers={"Accept": "application/json"}).json()
    assert res["found"] and res["slug"] == "one-piece"
    assert not any(h.startswith("/manga") for h in site.hits)                       # aucune recherche en direct


def make_setup_manga(ctx, name, title=None):
    from mangarr.autoadd import SETUP_MESSAGE
    mid = ctx.repo.add_manga(name, "lelscanfr", {"mangadex_title": title or name}, enabled=False)
    ctx.repo.set_status(mid, "error", SETUP_MESSAGE)
    return mid


def test_retry_finds_source_and_enables_manga(client, ctx, site):
    mid = make_setup_manga(ctx, "Blue_Lock", "Blue Lock")
    seed(ctx, ["Blue Lock"])
    r = client.post(f"/manga/{mid}/find-source")
    assert r.status_code == 303 and "src=ok" in r.headers["location"]
    m = ctx.repo.get_manga(mid)
    assert m["enabled"] and m["params"]["slug"] == "blue-lock" and m["source"] == "lelscanfr"
    assert ctx.repo.get_state(mid)["last_message"] == ""
    assert "source lelscanfr" in client.get(r.headers["location"]).text


def test_retry_failure_names_the_sources_searched(client, ctx, site):
    mid = make_setup_manga(ctx, "Introuvable")
    paged_site(site, [[("autre", "Autre")]])
    r = client.post(f"/manga/{mid}/find-source")
    assert "src=ko" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "aucune source ne propose ce manga" in page and "lelscanfr" in page
    assert not ctx.repo.get_manga(mid)["enabled"]
    assert client.post("/manga/999/find-source").status_code == 404


def test_retry_all_and_home_banner(client, ctx, site):
    a, b = make_setup_manga(ctx, "Blue_Lock", "Blue Lock"), make_setup_manga(ctx, "Pas_Present")
    seed(ctx, ["Blue Lock"])
    assert "2 manga(s)" in client.get("/").text and "Réessayer la recherche de source pour tous" in client.get("/").text
    paged_site(site, [])
    r = client.post("/retry-sources")
    assert r.status_code == 303 and r.headers["location"] == "/?retried=2&found=1"
    assert ctx.repo.get_manga(a)["enabled"] and not ctx.repo.get_manga(b)["enabled"]
    assert "1 source(s) trouvée(s) sur 2" in client.get(r.headers["location"]).text


# ------------------------------------------------------------------ jaquettes toujours présentes
def cbz(path, image=b"\xff\xd8\xff\xe0firstpage"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("002.jpg", b"\xff\xd8\xff\xe0second")
        z.writestr("001.jpg", image)


def test_fill_missing_covers_from_every_source(ctx, site, no_external_cover_download):
    lib = ctx.settings.library_dir
    a = ctx.repo.add_manga("Avec_Fichier", "pattern", {})
    os.makedirs(os.path.join(lib, "Avec_Fichier"))
    with open(os.path.join(lib, "Avec_Fichier", "cover.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0localcover")
    b = ctx.repo.add_manga("Avec_Tome", "pattern", {})
    cbz(os.path.join(lib, "Avec_Tome", "Avec_Tome_T02.cbz"), b"\xff\xd8\xff\xe0tome2")
    cbz(os.path.join(lib, "Avec_Tome", "Avec_Tome_T01.cbz"), b"\xff\xd8\xff\xe0tome1")
    c = ctx.repo.add_manga("Avec_Id", "pattern", {"mangadex_id": MID})
    ctx.settings.mangadex_api = site.url + "/md"
    site.add_json(f"/md/manga/{MID}", {"data": entry(1, "Avec Id")})
    d = ctx.repo.add_manga("Par_Nom", "pattern", {})
    site.add_json("/md/manga", {"data": [entry(2, "Par Nom")], "total": 1})
    e = ctx.repo.add_manga("Rien_Du_Tout", "pattern", {})

    assert covers.fill_missing(ctx) == 2                                          # fichier local + première page
    assert open(ctx.repo.get_manga(a)["cover"], "rb").read().endswith(b"localcover")
    assert open(ctx.repo.get_manga(b)["cover"], "rb").read().endswith(b"tome1")      # plus petit numéro de tome
    requested = {mid: url for mid, url in no_external_cover_download}
    assert requested[c].startswith("https://uploads.mangadex.org/covers/") and d in requested     # MangaDex : id puis nom
    assert e not in requested and not ctx.repo.get_manga(e)["cover"]
    assert covers.fill_missing(ctx) == 0                                          # idempotent : plus rien à faire pour a et b


def test_ensure_async_runs_once_in_background(ctx, monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(covers, "fill_missing", lambda c, limit=30: ran.set() or 0)
    monkeypatch.setitem(covers._LAST, "t", 0.0)
    assert REAL_ENSURE(ctx, force=True) is False                                  # aucun manga : rien à compléter
    ctx.repo.add_manga("Sans_Jaquette", "pattern", {})
    assert REAL_ENSURE(ctx) is True and ran.wait(3)
    covers._RUN.acquire(); covers._RUN.release()                                  # attend la fin du fil
    assert REAL_ENSURE(ctx) is False                                              # trop tôt : pas de nouvelle passe


def test_home_and_grid_trigger_cover_fill(client, no_background_work):
    client.get("/")
    client.get("/grid")
    assert len(no_background_work["covers"]) == 2
    assert client.post("/covers/refresh").status_code == 303 and no_background_work["covers"][-1] is True


# ------------------------------------------------------------------ pages
def test_settings_and_sources_pages(client, ctx, site, no_background_work):
    client.post("/settings/discover", data={"only_sources": "on"})
    assert 'name="only_sources" checked' in client.get("/settings").text
    client.post("/settings/discover", data={})
    assert ctx.repo.get_setting("discover_only_sources") is False
    assert 'name="only_sources" checked' not in client.get("/settings").text
    client.post("/settings/discover", data={"only_sources": "on"})
    assert ctx.repo.get_setting("discover_only_sources") is True
    paged_site(site, [[("a", "A")]])
    catalog.refresh(ctx)
    t = client.get("/sources").text
    assert "Catalogue des sources" in t and "lelscanfr" in t and "Actualiser" in t
    client.post("/catalog/refresh", data={"source": "lelscanfr"})
    client.post("/catalog/refresh", data={"source": "../x"})
    assert no_background_work["catalog"][-2:] == ["lelscanfr", None]


def test_new_routes_need_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    for method, url in (("post", "/retry-sources"), ("post", "/manga/1/find-source"), ("post", "/covers/refresh"),
                        ("post", "/catalog/refresh"), ("post", "/settings/discover")):
        assert getattr(c, method)(url).status_code == 303
