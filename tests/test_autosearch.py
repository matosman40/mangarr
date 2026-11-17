import pytest
from fastapi.testclient import TestClient

from mangarr.core.net import make_session
from mangarr.sources import OVERRIDES, SOURCES, autosearch, searchable_names
from mangarr.web.app import create_app

PAGE = "https://site.example/?s=one"

MADARA = """
<nav><a href="/genre/action/"><img src="/i/g.png" alt="Action"></a><a href="/"><img src="/logo.png" alt="logo"></a></nav>
<div class="c"><a href="https://site.example/manga/one-piece/" title="One Piece"><img data-src="/c/op.jpg" alt="x"></a></div>
<div class="c"><a href="/manga/one-punch-man/"><img src="/c/opm.jpg" alt="One Punch Man"></a></div>
<div class="c"><a href="/manga/one-piece/chapter-12/"><img src="/c/ch.jpg" alt="chapitre"></a></div>
<div class="c"><a href="https://autre.example/manga/pirate/"><img src="/c/p.jpg" alt="Pirate"></a></div>
<div class="c"><a href="/manga/one-piece/"><img src="/c/dup.jpg" alt="doublon"></a></div>
<a href="/manga/texte-seul/">sans image</a>
<div class="c"><a href="/manga/blue-lock.html"><img src="data:image/gif;base64,AAA" data-lazy-src="/c/bl.webp"><h3>Blue Lock</h3></a></div>
"""


def test_extract_cards_generic_markup():
    cards = autosearch.extract_cards(MADARA, PAGE)
    assert [c["slug"] for c in cards] == ["one-piece", "one-punch-man", "blue-lock"]
    assert cards[0]["title"] == "One Piece" and cards[0]["cover"] == "https://site.example/c/op.jpg"
    assert cards[1]["title"] == "One Punch Man" and cards[2]["title"] == "Blue Lock"
    assert cards[2]["cover"] == "https://site.example/c/bl.webp"          # data: ignoré, data-lazy-src retenu


def test_extract_cards_ignores_noise():
    assert autosearch.extract_cards("<html>rien ici</html>", PAGE) == []
    assert autosearch.extract_cards('<a href="javascript:x"><img src="a.jpg"></a>', PAGE) == []
    assert autosearch.extract_cards('<a href="/"><img src="a.jpg"></a>', PAGE) == []


def test_pagination_only_when_template_has_page(server):
    server.routes["/s"] = (200, "text/html", MADARA.replace("https://site.example", server.url).encode())
    assert autosearch.search_page(make_session(), server.url + "/s?q={q}", "x", page=2) == []
    assert len(autosearch.search_page(make_session(), server.url + "/s?q={q}&p={page}", "x", page=2)) == 3


def page_for(server, query_map):
    """Route dynamique : liste différente selon le texte cherché (vraie recherche)."""
    def route(path):
        for key, items in query_map.items():
            if key in path:
                cards = "".join(f'<a href="{server.url}/manga/{s}/"><img src="/c/{s}.jpg" alt="{t}"></a>' for s, t in items)
                return 200, "text/html", cards.encode()
        return 200, "text/html", b"<html>aucun resultat</html>"
    return route


def test_detect_finds_the_working_search_pattern(server):
    server.routes["/"] = page_for(server, {"s=one": [("one-piece", "One Piece"), ("one-punch", "One Punch")]})
    url, hits = autosearch.detect(make_session(), server.url)
    assert url.startswith(server.url + "/?s={q}") and {h["slug"] for h in hits} == {"one-piece", "one-punch"}


def test_detect_prefers_the_pattern_that_filters(server):
    everything = [("a", "A"), ("b", "B"), ("c", "C")]
    server.routes["/manga"] = page_for(server, {"": everything})                  # ignore le texte : catalogue fixe
    server.routes["/search"] = page_for(server, {"q=one": [("one-piece", "One Piece")]})
    url, hits = autosearch.detect(make_session(), server.url)
    assert url == server.url + "/search?q={q}"


def test_detect_returns_nothing_without_real_search(server):
    server.routes["/"] = page_for(server, {"": [("a", "A"), ("b", "B")]})          # même liste quoi qu'on cherche
    assert autosearch.detect(make_session(), server.url) == (None, [])
    with pytest.raises(ValueError):
        autosearch.detect(make_session(), "ftp://x")


# ------------------------------------------------------------------ interface
@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def test_sources_page_offers_search_setup_for_every_source(client):
    t = client.get("/sources").text
    assert "/sources/animesama/search" in t and "/sources/lelscans/search" in t and "/sources/patreon/search" not in t
    assert client.get("/sources/animesama/search").status_code == 200
    assert client.get("/sources/inconnue/search").status_code == 404


def test_configure_search_of_a_builtin_source_end_to_end(client, ctx, server):
    server.routes["/"] = page_for(server, {"zzqxjkw": [], "s=": [("boruto", "Boruto")]})
    assert "animesama" not in searchable_names()
    d = client.post("/sources/animesama/search/detect", data={"site": server.url})
    assert "recherche détectée" in d.text and f'value="{server.url}/?s={{q}}' in d.text and "hx-swap-oob" in d.text
    form = {"search_url": server.url + "/?s={q}", "t_q": "boruto"}
    assert "1 résultat" in client.post("/sources/animesama/search/test", data=form).text and "Boruto" in \
        client.post("/sources/animesama/search/test", data=form).text
    assert client.post("/sources/animesama/search", data=form).status_code == 303
    assert "animesama" in searchable_names() and "animesama" in OVERRIDES
    assert "Boruto" in client.get("/browse?source=animesama&q=boruto").text            # sans expression régulière
    assert "✅" in client.get("/sources").text
    # persiste après rechargement des sources (redémarrage)
    OVERRIDES.clear()
    ctx.reload_sources()
    assert "animesama" in searchable_names()
    client.post("/sources/animesama/search", data={"reset": "1"})
    assert "animesama" not in searchable_names() and not ctx.repo.get_setting("source_overrides")


def test_search_setup_validation(client, server):
    bad = {
        "ftp://x/?s={q}": "http",
        "https://x/?s=": "{q}",
    }
    for url, msg in bad.items():
        r = client.post("/sources/animesama/search", data={"search_url": url})
        assert r.status_code == 400 and msg in r.text
    r = client.post("/sources/animesama/search", data={"search_url": "https://x/?s={q}", "result_regex": "(("})
    assert r.status_code == 400 and "invalide" in r.text
    r = client.post("/sources/animesama/search", data={"search_url": "https://x/?s={q}", "result_regex": "(?P<title>x)"})
    assert r.status_code == 400 and "slug" in r.text
    assert "adresse" in client.post("/sources/animesama/search/test", data={}).text
    assert "invalide" in client.post("/sources/animesama/search/detect", data={"site": "ftp://x"}).text


def test_detect_failure_message(client, server):
    server.routes["/"] = (200, "text/html", b"<html>JS only</html>")
    t = client.post("/sources/lelscans/search/detect", data={"site": server.url}).text
    assert "aucune recherche fiable" in t and "hx-swap-oob" not in t


def test_search_setup_needs_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    for m, u in (("get", "/sources/animesama/search"), ("post", "/sources/animesama/search"),
                 ("post", "/sources/animesama/search/test"), ("post", "/sources/animesama/search/detect")):
        assert getattr(c, m)(u).status_code == 303
