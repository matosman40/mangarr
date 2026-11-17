import os

import pytest
from fastapi.testclient import TestClient

from mangarr.web.app import create_app
from tests.test_engine import add


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def test_health_and_empty_grid(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "Aucun manga suivi" in r.text


def test_grid_detail_cover_and_actions(client, ctx, server):
    server.add_chapter(1, 2)
    mid = add(ctx, server, "Mon_Manga", tomes_enabled=False)
    assert "Mon Manga" in client.get("/").text
    assert client.get(f"/manga/{mid}").status_code == 200
    assert client.get("/manga/999").status_code == 404
    assert client.get(f"/cover/{mid}").status_code == 404
    r = client.post(f"/manga/{mid}/check")            # sans planificateur : exécution directe
    assert r.status_code == 303
    assert ctx.repo.get_state(mid)["last_chapter"] == 1
    assert "à jour" in client.get("/grid").text
    assert "chapitre" in client.get("/logs").text
    client.post(f"/manga/{mid}/toggle")
    assert not ctx.repo.get_manga(mid)["enabled"] and "suspendu" in client.get("/grid").text
    client.post("/settings/interval", data={"minutes": "45"})
    assert ctx.repo.get_setting("check_interval_min") == 45
    client.post(f"/manga/{mid}/delete")
    assert ctx.repo.get_manga(mid) is None


def test_delete_keeps_files(client, ctx, server):
    server.add_chapter(1, 1)
    mid = add(ctx, server, "Keep_Files", tomes_enabled=False)
    client.post(f"/manga/{mid}/check")
    folder = os.path.join(ctx.settings.chapters_dir, "Keep_Files")
    client.post(f"/manga/{mid}/delete")
    assert os.path.isfile(os.path.join(folder, "Keep_Files_1.cbz"))


def test_auth_required_when_password_set(ctx):
    c = TestClient(create_app(ctx, password="secret"), follow_redirects=False)
    assert c.get("/health").status_code == 200
    assert c.get("/").headers["location"] == "/login"
    assert c.post("/check").status_code == 303        # redirigé vers /login, rien d'exécuté
    assert c.get("/cover/1").status_code == 303
    assert "incorrect" in c.post("/login", data={"password": "nope"}).text
    r = c.post("/login", data={"password": "secret"})
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/").status_code == 200
    c.cookies.clear()
    c.cookies.set("mangarr_session", "9999999999.forged")
    assert c.get("/").status_code == 303
