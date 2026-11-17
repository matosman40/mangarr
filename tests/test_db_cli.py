import os

from mangarr.cli import main
from mangarr.core.db import Database, Repo


def test_migrations_idempotent(tmp_path):
    p = str(tmp_path / "x.db")
    Database(p).close()
    db = Database(p)
    assert db.one("PRAGMA user_version")["user_version"] >= 1


def test_repo_crud(tmp_path):
    r = Repo(Database(str(tmp_path / "x.db")))
    i = r.add_manga("One", "html", {"a": 1})
    assert r.get_manga("one")["params"] == {"a": 1}
    r.update_manga(i, enabled=False)
    assert not r.list_mangas(only_enabled=True)
    r.save_last_chapter(i, 5)
    r.save_last_chapter(i, 3)
    assert r.get_state(i)["last_chapter"] == 5
    r.delete_manga(i)
    assert r.get_state(i) == {}


def test_cli_add_list_run(tmp_path, monkeypatch, server):
    monkeypatch.setenv("MANGARR_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("MANGARR_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.setenv("MANGARR_CHAPTER_DELAY", "0")
    monkeypatch.setenv("MANGARR_PAGE_DELAY", "0")
    server.add_chapter(1, 2)
    assert main(["add", "Cli_Manga", "--source", "pattern", "--param", f"base_img_url={server.url}/img"]) == 0
    assert main(["list"]) == 0
    assert main(["--only", "Cli_Manga", "--no-tomes"]) == 0
    assert os.path.isfile(tmp_path / "lib" / "_chapters" / "Cli_Manga" / "Cli_Manga_1.cbz")
    assert main(["--only", "Inconnu"]) == 2
    assert main(["sources"]) == 0
