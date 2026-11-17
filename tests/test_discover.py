from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mangarr.sources import SOURCES
from mangarr.web.app import create_app

MID = "11111111-2222-3333-4444-555555555555"
COVER = "https://uploads.mangadex.org/covers/" + MID + "/c.jpg.512.jpg"
LONG = "Un très long synopsis. " * 40


def entry(i=1, title="One Piece", desc=None, tags=("Action", "Comedy"), status="ongoing", lang="ja"):
    return {"id": f"{i:08d}-2222-3333-4444-555555555555",
            "attributes": {"title": {"en": title}, "description": desc if desc is not None else {"en": "English text"},
                           "year": 1997, "status": status, "originalLanguage": lang, "lastChapter": "1100",
                           "publicationDemographic": "shounen", "contentRating": "safe",
                           "tags": [{"attributes": {"group": "genre", "name": {"en": t}}} for t in tags]
                           + [{"attributes": {"group": "format", "name": {"en": "Long Strip"}}}]},
            "relationships": [{"type": "cover_art", "attributes": {"fileName": "c.jpg"}}]}


@pytest.fixture
def client(ctx, server):
    ctx.settings.mangadex_api = server.url + "/md"
    server.add_json("/md/manga/tag", {"data": [
        {"id": "t-action", "attributes": {"group": "genre", "name": {"en": "Action"}}},
        {"id": "t-comedy", "attributes": {"group": "genre", "name": {"en": "Comedy"}}},
        {"id": "t-isekai", "attributes": {"group": "theme", "name": {"en": "Isekai"}}},
        {"id": "t-long", "attributes": {"group": "format", "name": {"en": "Long Strip"}}}]})
    server.add_json("/md/manga", {"data": [entry()], "total": 1})
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def last_query(server):
    return parse_qs(urlparse([h for h in server.hits if h.startswith("/md/manga?")][-1]).query)


def test_page_lists_french_tags_and_filters(client):
    r = client.get("/discover")
    assert r.status_code == 200
    assert "Comédie" in r.text and "Isekai" in r.text and "Long Strip" not in r.text   # genres + thèmes uniquement
    assert "Dernières sorties" in r.text and "Manhwa" in r.text and "En pause" in r.text


def test_results_card_content(client):
    r = client.get("/discover/results")
    assert "One Piece" in r.text and "English text" in r.text and "1997" in r.text
    assert "Comédie" in r.text and "Manga" in r.text and "En cours" in r.text and "ch. 1100" in r.text
    assert "https://uploads.mangadex.org/covers/00000001-2222-3333-4444-555555555555/c.jpg.512.jpg" in r.text
    assert "/add?mdid=00000001-2222-3333-4444-555555555555&amp;mdtitle=One%20Piece" in r.text


def test_french_synopsis_preferred_and_long_text_collapsed(client, server):
    server.add_json("/md/manga", {"data": [entry(desc={"en": "english", "fr": LONG})], "total": 1})
    r = client.get("/discover/results")
    assert "Lire la suite" in r.text and "…" in r.text and LONG.strip() in r.text and "english" not in r.text
    server.add_json("/md/manga", {"data": [entry(desc={})], "total": 1})
    assert "Pas de synopsis" in client.get("/discover/results?q=x").text


def test_filters_become_api_parameters(client, server):
    client.get("/discover/results?q=blue&tag=t-action&tag=t-isekai&origin=ko&status=ongoing&status=hiatus"
               "&demo=seinen&order=rating&fr=1")
    q = last_query(server)
    assert q["title"] == ["blue"] and q["includedTags[]"] == ["t-action", "t-isekai"]
    assert q["includedTagsMode"] == ["AND"] and q["originalLanguage[]"] == ["ko"]
    assert q["status[]"] == ["ongoing", "hiatus"] and q["publicationDemographic[]"] == ["seinen"]
    assert q["availableTranslatedLanguage[]"] == ["fr"] and "order[rating]" in q and q["order[rating]"] == ["desc"]
    assert q["contentRating[]"] == ["safe", "suggestive"]                 # adulte exclu par défaut
    assert q["hasAvailableChapters"] == ["true"] and q["includes[]"] == ["cover_art"]


def test_adult_any_tag_and_bad_values(client, server):
    client.get("/discover/results?tag=t-action&any_tag=1&adult=1&origin=xx&status=bogus&order=hack&demo=bogus")
    q = last_query(server)
    assert q["includedTagsMode"] == ["OR"] and "erotica" in q["contentRating[]"]
    assert "originalLanguage[]" not in q and "status[]" not in q and "publicationDemographic[]" not in q
    assert "order[followedCount]" in q                                    # tri inconnu : valeur sûre par défaut
    client.get("/discover/results?order=title")
    assert last_query(server)["order[title]"] == ["asc"]


def test_pagination_keeps_filters(client, server):
    server.add_json("/md/manga", {"data": [entry(i) for i in range(1, 25)], "total": 60})
    r = client.get("/discover/results?origin=ja&tag=t-action&tag=t-comedy")
    assert "60 résultat" in r.text and "offset=24" in r.text and "tag=t-comedy" in r.text and "origin=ja" in r.text
    r2 = client.get("/discover/results?origin=ja&offset=48")
    assert last_query(server)["offset"] == ["48"] and "résultat(s)" not in r2.text and "offset=72" not in r2.text
    server.add_json("/md/manga", {"data": [entry()], "total": 24})
    assert "Voir plus" not in client.get("/discover/results?q=unique").text


def test_followed_marked(client, ctx):
    ctx.repo.add_manga("One_Piece", "lelscanfr", {"mangadex_id": "00000001-2222-3333-4444-555555555555"})
    r = client.get("/discover/results")
    assert "Déjà dans votre liste" in r.text and "/add?mdid" not in r.text


def test_errors_and_cache(client, server):
    n = len(server.hits)
    client.get("/discover/results?q=cache")
    client.get("/discover/results?q=cache")
    assert len([h for h in server.hits[n:] if h.startswith("/md/manga?")]) == 1      # 2e appel servi par le cache
    server.routes["/md/manga"] = (404, "text/plain", b"x")
    r = client.get("/discover/results?q=autre")
    assert r.status_code == 200 and "injoignable" in r.text
    server.routes["/md/manga/tag"] = (404, "text/plain", b"x")
    from mangarr.tomes import mangadex
    mangadex.CACHE.clear()
    assert "indisponible" in client.get("/discover").text


def test_follow_flow_prefills_add_page(client, server, monkeypatch):
    monkeypatch.setattr(SOURCES["lelscanfr"], "defaults", {**SOURCES["lelscanfr"].defaults,
                        "search_url": server.url + "/manga?title={q}&page={page}"})
    page = client.get(f"/add?mdid={MID}&mdtitle=One%20Piece&cover={COVER}")
    t = page.text
    assert "Associé à MangaDex" in t and f'value="{MID}"' in t and 'value="One_Piece"' in t
    assert f'name="cover_url" id="cover_url" value="{COVER}"' in t
    assert 'hx-trigger="submit, load"' in t and 'value="One Piece"' in t     # recherche sur les sites d'office
    # sans association valide : rien n'est repris
    bad = client.get("/add?mdid=../../etc&cover=http://evil/x.jpg&mdtitle=X").text
    assert "Associé à MangaDex" not in bad and "evil" not in bad
    assert 'value="https://uploads' not in client.get(f"/add?mdid={MID}&cover=https://evil.example/x.jpg").text


def test_browse_links_carry_the_association(client, server, monkeypatch):
    monkeypatch.setattr(SOURCES["lelscanfr"], "defaults", {**SOURCES["lelscanfr"].defaults,
                        "search_url": server.url + "/manga?title={q}&page={page}"})
    card = ('<a href="{u}/manga/one-piece"><figure><img data-src="/c.webp" alt="Titre du site" /></figure></a>'
            .format(u=server.url))
    server.routes["/manga"] = (200, "text/html", card.encode())
    r = client.get(f"/browse?source=lelscanfr&q=one&mdid={MID}&mdtitle=One%20Piece&cover={COVER}")
    assert f"mdid={MID}" in r.text and "slug=one-piece" in r.text
    assert "name=One%20Piece" in r.text and "Titre%20du%20site" not in r.text      # titre MangaDex prioritaire
    assert "mdid=" not in client.get("/browse?source=lelscanfr&q=one&mdid=pasvalide").text


def test_discover_needs_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    assert c.get("/discover").status_code == 303 and c.get("/discover/results").status_code == 303
