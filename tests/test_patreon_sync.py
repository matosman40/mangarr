import os
import time

import pytest
from fastapi.testclient import TestClient

from mangarr import patreon_sync
from mangarr.web.app import create_app

P1 = "https://www.patreon.com/creator/posts/titre-111111111"
P2 = "https://www.patreon.com/creator/posts/suite-222222222"


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password="secret"), follow_redirects=False)


def tok(ctx):
    return {"X-Mangarr-Token": patreon_sync.get_token(ctx.repo)}


def export(pid, server, camp="777", pages=2):
    return {"data": {"id": pid, "attributes": {"post_metadata": {"image_order": [str(i) for i in range(pages)]}},
                     "relationships": {"campaign": {"data": {"id": camp}}}},
            "included": [{"id": str(i), "type": "media",
                          "attributes": {"download_url": f"{server.url}/img/1/{i:02}.jpg"}} for i in range(pages)]}


def test_token_required(client, ctx):
    assert client.get("/patreon/wanted").status_code == 401
    assert client.post("/patreon/ingest", json={}).status_code == 401
    assert client.get("/patreon/wanted", headers={"X-Mangarr-Token": "faux"}).status_code == 401
    assert client.get("/patreon/userscript.user.js").status_code == 303          # script (avec jeton) : session requise


def test_userscript_contains_token_and_host(ctx):
    c = TestClient(create_app(ctx, password=""), follow_redirects=False)
    js = c.get("/patreon/userscript.user.js").text
    assert patreon_sync.get_token(ctx.repo) in js and "@connect      testserver" in js and "__TOKEN__" not in js


def test_wanted_then_ingest_downloads_automatically(client, ctx, server):
    server.add_chapter(1, 2)
    ctx.repo.add_manga("Serie_P", "patreon", {"posts": {"1": P1}})
    assert client.post("/login", data={"password": "secret"}).status_code == 303
    w = client.get("/patreon/wanted", headers=tok(ctx)).json()
    assert w["posts"] == ["111111111"] and "{id}" in w["post_api"] and w["campaigns"] == []
    r = client.post("/patreon/ingest", headers=tok(ctx), json={"exports": {"111111111": export("111111111", server)}})
    assert r.json()["saved"] == ["111111111"] and r.json()["campaigns"] == ["777"] and r.json()["mangas"] == ["Serie_P"]
    cbz = os.path.join(ctx.settings.library_dir, "Serie_P", "Serie_P_T01.cbz")
    for _ in range(100):                                    # le téléchargement part en arrière-plan
        if os.path.isfile(cbz):
            break
        time.sleep(0.1)
    assert os.path.isfile(cbz)
    assert client.get("/patreon/wanted", headers=tok(ctx)).json()["posts"] == []          # plus rien à exporter
    assert client.get("/patreon/wanted", headers=tok(ctx)).json()["campaigns"] == ["777"]


def test_bad_export_reported_not_fatal(client, ctx, server):
    r = client.post("/patreon/ingest", headers=tok(ctx),
                    json={"exports": {"1": {"nope": 1}, "222222222": export("222222222", server)}})
    assert r.status_code == 200 and "1" in r.json()["errors"] and r.json()["saved"] == ["222222222"]
    assert client.post("/patreon/ingest", headers=tok(ctx), content=b"pas du json").status_code == 400


def test_feed_news_and_add_post(ctx, server):
    c = TestClient(create_app(ctx, password=""), follow_redirects=False)
    ctx.settings.jikan_api = server.url
    mid = ctx.repo.add_manga("Serie_P", "patreon", {"posts": {"1": P1}})
    c.post("/patreon/ingest", headers=tok(ctx), json={"exports": {"111111111": export("111111111", server)}})
    feeds = {"777": [{"id": "111111111", "title": "Tome 1", "date": "2026-10-01T00:00:00", "url": P1},
                     {"id": "222222222", "title": "Tome 2 dispo", "date": "2026-11-10T00:00:00", "url": P2},
                     {"id": "333333333", "title": "Piège", "date": "2026-11-11", "url": "https://evil.example/x"}]}
    c.post("/patreon/ingest", headers=tok(ctx), json={"feeds": feeds})
    html = c.get(f"/manga/{mid}/news").text
    assert "Tome 2 dispo" in html and "Patreon" in html and "nouveau, pas encore dans la série" in html
    assert "evil.example" not in html
    page = c.get("/patreon").text
    assert "Nouveaux posts sur Patreon" in page and "Tome 2 dispo" in page and "Tome 1" not in page.split("Nouveaux posts")[1].split("</div>")[0]
    r = c.post("/patreon/add-post", data={"manga": str(mid), "url": P2})
    assert r.status_code == 303 and "tome%202" in r.headers["location"].lower()
    assert ctx.repo.get_manga(mid)["params"]["posts"]["2"] == P2
    again = c.post("/patreon/add-post", data={"manga": str(mid), "url": P2}).headers["location"]
    assert "d%c3%a9j%c3%a0" in again.lower()                      # « déjà dans la série » : pas de doublon
    assert len(ctx.repo.get_manga(mid)["params"]["posts"]) == 2
    assert "Tome 2 dispo" not in c.get("/patreon").text.split("Nouveaux posts")[1].split("</div>")[0]


def test_inbox_paste_and_status(ctx, server):
    import json as _json
    c = TestClient(create_app(ctx, password=""), follow_redirects=False)
    ctx.repo.add_manga("Serie_P", "patreon", {"posts": {"1": P1, "2": P2}})
    os.makedirs(patreon_sync.inbox_dir(ctx))
    with open(os.path.join(patreon_sync.inbox_dir(ctx), "nimporte quoi.json"), "w") as f:
        _json.dump(export("111111111", server), f)
    with open(os.path.join(patreon_sync.inbox_dir(ctx), "casse.json"), "w") as f:
        f.write("{pas du json")
    assert "script jamais vu" in c.get("/patreon").text             # l'inbox est lue à l'ouverture de la page
    assert os.path.isfile(os.path.join(ctx.settings.patreon_dir, "111111111.json"))
    assert not os.path.exists(os.path.join(patreon_sync.inbox_dir(ctx), "nimporte quoi.json"))
    assert os.path.exists(os.path.join(patreon_sync.inbox_dir(ctx), "casse.json"))     # invalide : conservé
    r = c.post("/patreon/paste", data={"text": _json.dumps(export("222222222", server))})
    assert "enregistr" in r.headers["location"]
    assert os.path.isfile(os.path.join(ctx.settings.patreon_dir, "222222222.json"))
    assert "Import%20impossible" in c.post("/patreon/paste", data={"text": "bla"}).headers["location"]
    assert "Import%20impossible" in c.post("/patreon/paste", data={"text": ""}).headers["location"]
    c.get("/patreon/wanted", headers=tok(ctx))
    assert "script actif" in c.get("/patreon").text
    assert c.post("/patreon/check").status_code == 303
    assert "export(s) Patreon" not in c.get("/").text               # tout est déjà exporté
