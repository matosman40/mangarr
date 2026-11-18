import pytest
from fastapi.testclient import TestClient

from mangarr import news
from mangarr.web.app import create_app
from tests.test_engine import add


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(news, "PAUSE", 0)


def jikan(server, ctx):
    ctx.settings.jikan_api = server.url
    st = ctx.settings
    st.reddit_url = st.reddit_oauth_url = server.url
    st.reddit_client_id, st.reddit_client_secret = "id", "secret"
    news._TOKEN.clear()
    server.routes[("POST", "/api/v1/access_token")] = (200, "application/json", b'{"access_token": "tok", "expires_in": 86400}')
    server.add_json("/manga", {"data": [{"mal_id": 7, "title": "Mon Manga", "titles": []}]})
    server.add_json("/manga/7/full", {"data": {
        "mal_id": 7, "url": "https://myanimelist.net/manga/7", "title": "Mon Manga", "status": "Publishing",
        "published": {"string": "2010 to ?"}, "genres": [{"name": "Action"}], "themes": [],
        "authors": [{"mal_id": 3, "name": "Auteur, Un", "url": "https://myanimelist.net/people/3"}]}})
    server.add_json("/manga/7/news", {"data": [
        {"title": "Adaptation annoncée", "url": "https://myanimelist.net/news/1", "date": "2026-11-01T10:00:00+00:00",
         "author_username": "bob", "excerpt": "Un anime arrive."},
        {"title": "Piégé", "url": "javascript:alert(1)", "date": "2026-10-01T00:00:00+00:00"}]})
    server.add_json("/manga/7/forum", {"data": [
        {"title": "Chapter 12 Discussion", "url": "https://myanimelist.net/forum/?topicid=1", "comments": 5,
         "date": "2026-11-02T00:00:00+00:00"}]})
    server.add_json("/r/manga/search", {"data": {"children": [
        {"data": {"title": "Mon Manga Chapter 12 Discussion", "permalink": "/r/manga/comments/x/", "score": 90,
                  "num_comments": 12, "created_utc": 1762000000, "link_flair_text": "Discussion", "selftext": ""}},
        {"data": {"title": "Autre chose", "permalink": "/r/manga/comments/y/", "created_utc": 1762000000}},
        {"data": {"title": "Mon Manga spoil", "permalink": "/r/manga/comments/z/", "spoiler": True,
                  "created_utc": 1762100000}}]}})
    server.add_json("/people/3", {"data": {"about": "Né à Tokyo.", "images": {"jpg": {}}}})


def test_news_section_and_caching(ctx, server):
    jikan(server, ctx)
    mid = add(ctx, server, "Mon_Manga", tomes_enabled=False)
    client = TestClient(create_app(ctx, password=""), follow_redirects=False)
    html = client.get(f"/manga/{mid}/news").text
    assert "Adaptation annoncée" in html and "Auteur, Un" in html and "Né à Tokyo" in html
    assert "Chapter 12 Discussion" in html and "spoilers possibles" in html and "Publishing" in html
    assert "Mon Manga Chapter 12 Discussion" in html and "Autre chose" not in html     # Reddit filtré sur le titre
    assert "reddit.com/r/manga/comments/x/" in html
    assert "javascript:" not in html                       # lien non-MAL retiré
    hits = len(server.hits)
    assert "Adaptation annoncée" in client.get(f"/manga/{mid}/news").text
    assert len(server.hits) == hits                        # 2e affichage : cache, aucun appel réseau
    client.get(f"/manga/{mid}/news?refresh=1")
    assert len(server.hits) > hits


def test_news_api_down_shows_error(ctx, server):
    ctx.settings.jikan_api = server.url                    # aucune route -> 404 -> manga introuvable
    mid = add(ctx, server, "Inconnu", tomes_enabled=False)
    client = TestClient(create_app(ctx, password=""), follow_redirects=False)
    r = client.get(f"/manga/{mid}/news")
    assert r.status_code == 200 and "indisponibles" in r.text
    assert client.get("/news").status_code == 200 and "Inconnu" in client.get("/news").text
    assert client.get("/manga/999/news").status_code == 404


def test_reddit_down_does_not_hide_mal(ctx, server):
    jikan(server, ctx)
    server.routes["/r/manga/search"] = (429, "text/plain", b"slow down")
    mid = add(ctx, server, "Mon_Manga", tomes_enabled=False)
    client = TestClient(create_app(ctx, password=""), follow_redirects=False)
    html = client.get(f"/manga/{mid}/news").text
    assert "Adaptation annoncée" in html and "Reddit indisponible" in html


def test_reddit_uses_official_api_with_token(ctx, server):
    jikan(server, ctx)
    mid = add(ctx, server, "Mon_Manga", tomes_enabled=False)
    TestClient(create_app(ctx, password=""), follow_redirects=False).get(f"/manga/{mid}/news")
    path, headers, body = server.posts[0]
    assert path == "/api/v1/access_token" and b"client_credentials" in body and headers["Authorization"].startswith("Basic ")


def test_reddit_not_configured_says_so(ctx, server):
    jikan(server, ctx)
    ctx.settings.reddit_client_id = ""
    mid = add(ctx, server, "Mon_Manga", tomes_enabled=False)
    html = TestClient(create_app(ctx, password=""), follow_redirects=False).get(f"/manga/{mid}/news").text
    assert "Reddit non configuré" in html and "Adaptation annoncée" in html
    assert not any("search" in h for h in server.hits)       # aucun appel non authentifié
