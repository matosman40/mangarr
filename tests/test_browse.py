import pytest
from fastapi.testclient import TestClient

from mangarr.core.net import make_session
from mangarr.sources import SOURCES, Source, register
from mangarr.web.app import create_app

CARD = ('<a href="{base}/manga/{slug}"><figure class="g"><img class="lazyload" data-src="/covers/{slug}.webp" '
        'alt="{title}" /></figure></a>')


def catalogue(server, items, path="/manga"):
    html = '<a href="https://x/manga/random" aria-label="r">hasard</a>' + "".join(
        CARD.format(base=server.url, slug=s, title=t) for s, t in items)
    server.routes[path] = (200, "text/html", html.encode())


@pytest.fixture
def client(ctx, server):
    # lelscanfr (intégrée) pointée vers le faux serveur
    SOURCES["lelscanfr"].defaults = {**SOURCES["lelscanfr"].defaults, "domain": server.url.replace("http://", "")}
    SOURCES["lelscanfr"].defaults["search_url"] = server.url + "/manga?title={q}&page={page}"
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def test_builtin_search_parses_cards(server, settings):
    catalogue(server, [("one-piece", "One Piece"), ("i-m-fine", "I&#039;m Fine")])
    src = SOURCES["lelscanfr"]({**SOURCES["lelscanfr"].defaults, "search_url": server.url + "/manga?title={q}&page={page}"})
    res = src.search(make_session(), "one", 1)
    assert [r["slug"] for r in res] == ["one-piece", "i-m-fine"]        # le lien « random » sans carte est ignoré
    assert res[1]["title"] == "I'm Fine" and res[0]["cover"] == server.url + "/covers/one-piece.webp"
    assert src.can_search()


def test_browse_all_sources_and_prefill(client, ctx, server):
    catalogue(server, [("one-piece", "One Piece"), ("blue-lock", "Blue Lock")])
    page = client.get("/add")
    assert "Tous les sites" in page.text and "lelscanfr" in page.text
    r = client.get("/browse?q=one")
    assert "One Piece" in r.text and "Blue Lock" in r.text and "/add?source=lelscanfr&amp;slug=one-piece" in r.text
    ctx.repo.add_manga("Blue_Lock", "lelscanfr", {"slug": "blue-lock"})
    r = client.get("/browse?source=lelscanfr")
    assert r.text.count("déjà suivi") == 1                              # seul Blue Lock est déjà suivi
    pre = client.get("/add?source=lelscanfr&slug=one-piece&name=One%20Piece")
    assert 'value="one-piece"' in pre.text and 'value="One_Piece"' in pre.text
    assert 'hx-trigger="submit, load"' in pre.text                      # recherche MangaDex lancée d'office


def test_browse_errors_are_shown_not_raised(client, server):
    server.routes["/manga"] = (403, "text/html", b"blocked")
    r = client.get("/browse?source=lelscanfr")
    assert r.status_code == 200 and "403" in r.text
    server.routes["/manga"] = (200, "text/html", b"<html>structure changee</html>")
    assert "Aucun résultat" in client.get("/browse?source=lelscanfr&q=zzz").text


def test_pagination_link(client, server):
    catalogue(server, [(f"m{i}", f"Manga {i}") for i in range(12)])
    assert "page=2" in client.get("/browse?source=lelscanfr").text
    catalogue(server, [("a", "A")])
    assert "page=2" not in client.get("/browse?source=lelscanfr").text


def test_plugin_search(client):
    @register("plug_search")
    class P(Source):
        def search(self, session, query, page=1):
            return [{"slug": "x1", "title": f"Résultat {query}", "cover": None}]

        def iter_images(self, session, params, chap):
            return iter([])

    r = client.get("/browse?source=plug_search&q=abc")
    assert "Résultat abc" in r.text
    assert "plug_search" in client.get("/add").text


def test_custom_source_with_search(client, ctx, server):
    server.routes["/cat"] = (200, "text/html", b'<li><a href="/s/foo-bar" title="Foo Bar">Foo</a></li>')
    data = {"name": "monsite", "kind": "html", "d_chapter_url": server.url + "/{slug}/{chap}",
            "d_search_url": server.url + "/cat?q={q}",
            "d_result_regex": r'<a href="/s/(?P<slug>[^"]+)" title="(?P<title>[^"]+)"'}
    t = client.post("/sources/test-search", data={**data, "t_q": "foo"}).text
    assert "1 résultat" in t and "Foo Bar" in t
    assert client.post("/sources", data=data).status_code == 303
    assert "Foo Bar" in client.get("/browse?source=monsite&q=foo").text
    assert "✅" in client.get("/sources").text


def test_search_config_validation(client):
    base = {"name": "okname", "kind": "html", "d_chapter_url": "http://x/{chap}"}
    r = client.post("/sources", data={**base, "d_search_url": "http://x/?q={q}"})
    assert r.status_code == 400 and "à la fois" in r.text
    r = client.post("/sources", data={**base, "d_search_url": "http://x/?q={q}", "d_result_regex": "(?P<title>x)"})
    assert r.status_code == 400 and "slug" in r.text
    r = client.post("/sources", data={**base, "d_search_url": "http://x/?q={q}", "d_result_regex": "(("})
    assert r.status_code == 400
    assert "recherche" in client.post("/sources/test-search", data={**base}).text


def test_browse_needs_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    assert c.get("/browse?q=x").status_code == 303
    assert c.post("/sources/test-search", data={}).status_code == 303
