import json
import os

import pytest
from fastapi.testclient import TestClient

from mangarr import notify
from mangarr.core.engine import check_all
from mangarr.importer import convert, import_mangas, parse_config
from mangarr.web.app import create_app
from tests.test_engine import add


# ------------------------------------------------------------------ notifications
def test_send_each_service(server):
    assert notify.send_one("discord", {"url": server.url + "/hook"}, "Titre", "Corps")[0]
    assert notify.send_one("telegram", {"token": "T0K", "chat_id": "42", "api_base": server.url}, "T", "M")[0]
    assert notify.send_one("ntfy", {"url": server.url + "/sujet", "token": "tk"}, "Titre é", "Corps")[0]
    paths = [p[0] for p in server.posts]
    assert paths == ["/hook", "/botT0K/sendMessage", "/sujet"]
    assert json.loads(server.posts[0][2])["content"].startswith("**Titre**")
    assert json.loads(server.posts[1][2]) == {"chat_id": "42", "text": "T\nM"}
    assert server.posts[2][1]["Authorization"] == "Bearer tk"


def test_send_failures_do_not_leak_secrets(server):
    server.routes[("POST", "/hook")] = (500, "text/plain", b"x")
    assert notify.send_one("discord", {"url": server.url + "/hook"}, "t", "m") == (False, "HTTP 500")
    ok, info = notify.send_one("discord", {"url": "http://127.0.0.1:1/secret-token"}, "t", "m")
    assert not ok and "secret-token" not in info
    assert not notify.send_one("discord", {"url": "file:///etc/passwd"}, "t", "m")[0]
    assert not notify.send_one("ntfy", {"url": "ftp://x"}, "t", "m")[0]


def test_notify_on_new_and_dedup_errors(ctx, server):
    ctx.repo.set_setting("notifications", {"discord": {"url": server.url + "/hook"}})
    server.add_chapter(1, 1)
    add(ctx, server, "Bon_Manga", tomes_enabled=False)
    server.routes["/bad/1/00.jpg"] = (403, "text/html", b"x")
    ctx.repo.add_manga("Bloque", "pattern", {"base_img_url": server.url + "/bad"}, tomes_enabled=False)
    check_all(ctx)
    texts = [json.loads(b)["content"] for _, _, b in server.posts]
    assert any("Bon Manga" in t and "chapitre(s) 1" in t for t in texts)
    assert any("Bloque" in t and "403" in t for t in texts)
    n = len(server.posts)
    check_all(ctx)                                  # même erreur, rien de nouveau : silence
    assert len(server.posts) == n


def test_no_notification_when_disabled_or_dry_run(ctx, server):
    from mangarr.core.engine import RunOptions
    ctx.repo.set_setting("notifications", {"discord": {"url": server.url + "/hook"}, "on_new": False,
                                           "on_error": False})
    server.add_chapter(1, 1)
    add(ctx, server, tomes_enabled=False)
    check_all(ctx)
    assert not server.posts
    ctx.repo.set_setting("notifications", {"discord": {"url": server.url + "/hook"}})
    check_all(ctx, opts=RunOptions(dry_run=True))
    assert not server.posts


# ------------------------------------------------------------------ bibliothèques
def test_komga_and_kavita_refresh(server):
    assert notify.refresh_one("komga", {"url": server.url, "library_id": "L1", "api_key": "K"})[0]
    path, headers, _ = server.posts[-1]
    assert path == "/api/v1/libraries/L1/scan" and headers["X-API-Key"] == "K"
    assert notify.refresh_one("komga", {"url": server.url, "library_id": "L2", "user": "u", "password": "p"})[0]
    assert server.posts[-1][1]["Authorization"].startswith("Basic ")

    server.routes[("POST", "/api/Plugin/authenticate")] = (200, "application/json", b'{"token": "JWT"}')
    assert notify.refresh_one("kavita", {"url": server.url, "library_id": "3", "api_key": "KK"})[0]
    path, headers, _ = server.posts[-1]
    assert path.startswith("/api/Library/scan?libraryId=3") and headers["Authorization"] == "Bearer JWT"
    assert not notify.refresh_one("kavita", {"url": server.url})[0]
    assert not notify.refresh_one("komga", {"url": "nope", "library_id": "1"})[0]


def test_refresh_after_new_content(ctx, server):
    ctx.repo.set_setting("integrations", {"komga": {"url": server.url, "library_id": "L9", "api_key": "K"}})
    server.add_chapter(1, 1)
    add(ctx, server, tomes_enabled=False)
    check_all(ctx)
    assert any(p[0] == "/api/v1/libraries/L9/scan" for p in server.posts)


# ------------------------------------------------------------------ pages réglages
@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def test_settings_secrets_never_echoed(client, ctx):
    assert client.get("/settings").status_code == 200
    r = client.post("/settings/notifications", data={
        "discord_url": "https://discord.com/api/webhooks/SECRETVALUE", "telegram_token": "TGSECRET",
        "telegram_chat_id": "99", "on_new": "on"})
    assert r.status_code == 303
    page = client.get("/settings").text
    assert "SECRETVALUE" not in page and "TGSECRET" not in page and "99" in page and "enregistré" in page
    client.post("/settings/notifications", data={"telegram_chat_id": "100", "on_new": "on", "on_error": "on"})
    cfg = ctx.repo.get_setting("notifications")
    assert cfg["discord"]["url"].endswith("SECRETVALUE") and cfg["telegram"]["chat_id"] == "100"
    assert cfg["telegram"]["token"] == "TGSECRET" and cfg["on_new"] and cfg["on_error"]
    client.post("/settings/notifications", data={"clear_discord": "on"})
    cfg = ctx.repo.get_setting("notifications")
    assert "discord" not in cfg and not cfg["on_new"]


def test_settings_test_buttons(client, ctx, server):
    assert "Aucun service" in client.post("/settings/notifications/test").text
    ctx.repo.set_setting("notifications", {"ntfy": {"url": server.url + "/s"}})
    assert "✅" in client.post("/settings/notifications/test").text
    client.post("/settings/library", data={"komga_url": server.url, "komga_library": "L", "komga_api_key": "K"})
    assert "analyse demandée" in client.post("/settings/library/test").text
    assert 'value="K"' not in client.get("/settings").text      # la clé n'est jamais réaffichée


# ------------------------------------------------------------------ import
OLD = '''
import os
ROOT = "/data/exemple"
MANGAS = [
    {"name": "One_Piece", "source": "lelscanfr", "slug": "one-piece", "start_chap": 1152},
    {"name": "Serie P", "source": "patreon", "start_chap": 3, "posts": ["https://www.patreon.com/c/posts/a-111111111",
                                                                   "https://www.patreon.com/c/posts/b-222222222"]},
    {"name": "Off", "source": "html", "enabled": False, "tomes": False, "folder": "/data/x", "chapter_url": "u{chap}"},
]
'''


def test_parse_never_executes(tmp_path):
    marker = tmp_path / "pwned"
    code = f"open(r'{marker}', 'w').write('x')\nMANGAS = [{{'name': 'A'}}]\n"
    assert parse_config(code) == [{"name": "A"}]
    assert not marker.exists()
    with pytest.raises(ValueError, match="littérales"):
        parse_config("MANGAS = [dict(name=open('x'))]")
    with pytest.raises(ValueError, match="introuvable"):
        parse_config("X = 1")
    with pytest.raises(ValueError, match="invalide"):
        parse_config("def (")


def test_convert_and_import(ctx, tmp_path):
    mangas = parse_config(OLD)
    kw = convert(mangas[1])
    assert kw["name"] == "Serie_P" and kw["params"]["posts"] == {"3": "https://www.patreon.com/c/posts/a-111111111",
                                                                  "4": "https://www.patreon.com/c/posts/b-222222222"}
    sim = import_mangas(ctx, mangas)
    assert [r["action"] for r in sim] == ["serait ajouté"] * 3 and not ctx.repo.list_mangas()

    state = tmp_path / "chapters" / "One_Piece"
    state.mkdir(parents=True)
    (state / ".mangadl_state.json").write_text('{"last_chapter": 1160}')
    exports = tmp_path / "exp"
    exports.mkdir()
    (exports / "111111111.json").write_text("{}")
    (exports / "notes.json").write_text("{}")
    rows = import_mangas(ctx, mangas, apply=True, chapters_root=str(tmp_path / "chapters"), patreon_dir=str(exports))
    assert [r["action"] for r in rows] == ["ajouté"] * 3
    op = ctx.repo.get_manga("One_Piece")
    assert op["params"] == {"slug": "one-piece"} and ctx.repo.get_state(op["id"])["last_chapter"] == 1160
    off = ctx.repo.get_manga("Off")
    assert not off["enabled"] and not off["tomes_enabled"] and off["folder"] is None     # chemin perso non repris
    assert sorted(os.listdir(ctx.settings.patreon_dir)) == ["111111111.json"]
    assert import_mangas(ctx, mangas, apply=True)[0]["action"] == "existe déjà"
    kept = import_mangas(ctx, [{"name": "Z", "folder": "/a", "tomes_folder": "/b"}], apply=True, keep_folders=True)
    assert ctx.repo.get_manga("Z")["folder"] == "/a" and kept[0]["action"] == "ajouté"


def test_import_page(client, ctx):
    assert client.get("/import").status_code == 200
    files = {"file": ("manga_dl.py", OLD.encode(), "text/x-python")}
    r = client.post("/import", files=files)
    assert "serait ajouté" in r.text and not ctx.repo.list_mangas()
    r = client.post("/import", files=files, data={"apply": "on"})
    assert "ajouté" in r.text and len(ctx.repo.list_mangas()) == 3
    r = client.post("/import", files={"file": ("x.py", b"X = 1", "text/x-python")})
    assert r.status_code == 400 and "introuvable" in r.text


def test_cli_import(tmp_path, monkeypatch, capsys):
    from mangarr.cli import main
    monkeypatch.setenv("MANGARR_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("MANGARR_CONFIG", str(tmp_path / "cfg"))
    f = tmp_path / "old.py"
    f.write_text(OLD)
    assert main(["import", str(f)]) == 0
    assert "Simulation" in capsys.readouterr().out
    assert main(["import", str(f), "--apply"]) == 0
    assert main(["list"]) == 0
    assert "One_Piece" in capsys.readouterr().out
    assert main(["import", str(tmp_path / "absent.py")]) == 2


def test_new_pages_need_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    for url in ("/settings", "/import"):
        assert c.get(url).status_code == 303
    assert c.post("/settings/notifications", data={}).status_code == 303
    assert c.post("/import", files={"file": ("a.py", b"x", "text/plain")}).status_code == 303
