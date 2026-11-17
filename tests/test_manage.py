import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from mangarr.sources import SOURCES
from mangarr.web.app import create_app
from tests.conftest import jpeg

POST1 = "https://www.patreon.com/creator/posts/titre-111111111"
POST2 = "https://www.patreon.com/creator/posts/autre-222222222"


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def form(server, **kw):
    d = {"name": "Mon Manga", "source": "pattern", "start_chap": "1", "chap": "1",
         "p_base_img_url": server.url + "/img", "tomes_enabled": "on"}
    d.update(kw)
    return d


def test_pages_render(client):
    for url in ("/add", "/sources", "/sources/new", "/patreon", "/add/fields?source=patreon",
                "/add/fields?source=html", "/sources/fields?kind=pattern"):
        assert client.get(url).status_code == 200, url


def test_search_mangadex(client, ctx, server):
    ctx.settings.mangadex_api = server.url + "/md"
    server.add_json("/md/manga", {"data": [{"id": "abc", "attributes": {"title": {"en": "Blue Lock"}, "year": 2018},
                                            "relationships": [{"type": "cover_art",
                                                               "attributes": {"fileName": "c.jpg"}}]}]})
    r = client.get("/add/search?q=blue")
    assert "Blue Lock" in r.text and 'data-id="abc"' in r.text and "uploads.mangadex.org/covers/abc/c.jpg" in r.text
    server.routes["/md/manga"] = (404, "text/plain", b"boom")
    assert "impossible" in client.get("/add/search?q=x").text


def test_test_button(client, server):
    server.add_chapter(1, 3)
    assert "3 image" in client.post("/add/test", data=form(server)).text
    assert "introuvable" in client.post("/add/test", data=form(server, chap="9")).text
    assert "JSON invalide" in client.post("/add/test", data=form(server, extra="{nope")).text
    server.routes["/img/1/00.jpg"] = (403, "text/html", b"x")
    assert "403" in client.post("/add/test", data=form(server)).text


def test_add_manga_full_flow(client, ctx, server):
    server.add_chapter(1, 1)
    server.routes["/cov.jpg"] = (200, "image/jpeg", jpeg("cover"))
    r = client.post("/add", data=form(server, name="Mon Manga/../x", mangadex_id="abc", cover_url="http://evil/x.jpg",
                                       extra='{"keep_chapters": true}', folder="/tmp/x"))
    assert r.status_code == 303
    m = ctx.repo.get_manga("Mon_Manga..x")
    assert m["params"] == {"base_img_url": server.url + "/img", "mangadex_id": "abc", "keep_chapters": True}
    assert m["folder"] == "/tmp/x" and m["tomes_enabled"] and not m["cover"]   # URL de jaquette hors MangaDex ignorée
    assert not any("/cov.jpg" in h or "evil" in h for h in server.hits)
    assert client.post("/add", data=form(server, name="Mon Manga/../x")).status_code == 400   # doublon


def test_add_validation_errors(client, server):
    assert "obligatoire" in client.post("/add", data=form(server, name="///")).text
    assert "source inconnue" in client.post("/add", data=form(server, source="nope")).text
    r = client.post("/add", data=form(server, extra="[1]"))
    assert r.status_code == 400 and "objet JSON" in r.text
    r = client.post("/add", data=form(server, source="patreon", posts_text="1 pas-une-url"))
    assert r.status_code == 400 and "invalide" in r.text


def test_edit_manga(client, ctx, server):
    client.post("/add", data=form(server))
    m = ctx.repo.get_manga("Mon_Manga")
    page = client.get(f"/manga/{m['id']}/edit")
    assert page.status_code == 200 and server.url in page.text
    r = client.post(f"/manga/{m['id']}/edit", data=form(server, name="IGNORE", start_chap="7", p_page_start="1"))
    assert r.status_code == 303
    m2 = ctx.repo.get_manga(m["id"])
    assert m2["name"] == "Mon_Manga" and m2["start_chap"] == 7 and m2["params"]["page_start"] == 1
    assert not m2["enabled"]   # case « actif » décochée dans ce formulaire
    assert client.get("/manga/999/edit").status_code == 404


def test_patreon_manga_posts_parsed(client, ctx):
    r = client.post("/add", data={"name": "Serie P", "source": "patreon", "start_chap": "1",
                                  "posts_text": f"1 {POST1}\n2 {POST2}\n"})
    assert r.status_code == 303
    assert ctx.repo.get_manga("Serie_P")["params"]["posts"] == {"1": POST1, "2": POST2}


def test_custom_source_lifecycle(client, ctx, server):
    server.add_chapter(1, 2, base="/mine/foo")
    base = {"name": "monsite", "kind": "pattern", "d_base_img_url": server.url + "/mine/{slug}",
            "description": "mon site"}
    assert "2 image" in client.post("/sources/test", data={**base, "t_slug": "foo", "chap": "1"}).text
    assert client.post("/sources", data=base).status_code == 303
    assert "monsite" in SOURCES and "monsite" in client.get("/sources").text
    assert client.get("/sources/monsite/edit").status_code == 200
    ctx.repo.add_manga("Via", "monsite", {"slug": "foo"})
    assert client.post("/sources/monsite/delete").status_code == 409     # utilisée
    ctx.repo.delete_manga(ctx.repo.get_manga("Via")["id"])
    assert client.post("/sources/monsite/delete").status_code == 303
    assert "monsite" not in SOURCES


def test_custom_source_validation(client):
    for data, msg in [({"name": "Bad Name!", "kind": "html"}, "nom invalide"),
                      ({"name": "lelscans", "kind": "html"}, "existe déjà"),
                      ({"name": "okname", "kind": "html"}, "obligatoire"),
                      ({"name": "okname", "kind": "html", "d_chapter_url": "x", "d_img_regex": "("}, "")]:
        r = client.post("/sources", data=data)
        assert r.status_code == 400 and msg in r.text
    assert client.post("/sources/lelscans/delete").status_code == 404     # intégrée : non supprimable


def export(pid, urls):
    return {"data": {"id": pid, "attributes": {"post_metadata": {"image_order": [str(i) for i in range(len(urls))]}}},
            "included": [{"id": str(i), "type": "media", "attributes": {"download_url": u}}
                         for i, u in enumerate(urls)]}


def upload(client, name, content):
    raw = content if isinstance(content, bytes) else json.dumps(content).encode()
    return client.post("/patreon/upload", files=[("files", (name, raw, "application/json"))]).json()[0]


def test_patreon_page_and_upload(client, ctx, settings):
    client.post("/add", data={"name": "Serie P", "source": "patreon", "posts_text": f"1 {POST1}\n2 {POST2}"})
    page = client.get("/patreon").text
    assert "2 export(s) à faire" in page and "manquant" in page and "api/posts/111111111" in page

    soon = int(time.time()) + 86400
    res = upload(client, "../../evil.json", export("111111111", [f"https://x.invalid/a.jpg?token-time={soon}"]))
    assert res["ok"] and res["id"] == "111111111"
    assert os.path.isfile(os.path.join(settings.patreon_dir, "111111111.json"))
    assert not os.path.exists(os.path.join(os.path.dirname(settings.patreon_dir), "evil.json"))

    old = int(time.time()) - 3600
    assert upload(client, "b.json", export("222222222", [f"https://x.invalid/a.jpg?token-time={old}"]))["ok"]
    page = client.get("/patreon").text
    assert "prêt (1 images)" in page and "expiré" in page and "1 export(s) à faire" in page

    assert "id du post" in upload(client, "c.json", {"data": {}})["message"]
    assert not upload(client, "d.json", b"not json")["ok"]
    assert "aucune image" in upload(client, "e.json", export("333333333", []))["message"]
    assert "aucune série" in upload(client, "f.json", export("999999999", ["https://x.invalid/a.jpg"]))["message"]
    assert "trop gros" in upload(client, "g.json", b" " * (5 * 1024 * 1024 + 10))["message"]


def test_new_pages_need_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    for url in ("/add", "/sources", "/patreon", "/add/search?q=x"):
        assert c.get(url).status_code == 303
    assert c.post("/patreon/upload", files=[("files", ("a.json", b"{}", "application/json"))]).status_code == 303
    assert c.post("/add/test", data={}).status_code == 303
