import io
import os
import sqlite3
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from mangarr import health
from mangarr.web.app import create_app
from tests.test_engine import add


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def make_cbz(path, pages=2, corrupt=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if corrupt:
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04pas une archive")
        return
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i:03}.jpg", b"\xff\xd8\xff\xe0" + bytes(20))


def test_pages_render(client):
    for url in ("/status", "/quality", "/calendar", "/quality/result", "/static/manifest.webmanifest",
                "/static/icon-180.png", "/static/icon-512.png"):
        assert client.get(url).status_code == 200, url
    html = client.get("/").text
    assert "manifest.webmanifest" in html and "apple-touch-icon" in html and "diagnostic de l'installation" in html


def test_checks_detect_unwritable_folder(ctx, monkeypatch):
    assert all(i["level"] != "bad" for i in health.checks(ctx))
    real = os.makedirs

    def deny(path, *a, **k):
        if str(path).endswith("patreon"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(health.os, "makedirs", deny)
    bad = [i for i in health.checks(ctx) if i["level"] == "bad"]
    assert len(bad) == 1 and "Exports Patreon" in bad[0]["label"] and "Permission denied" in bad[0]["detail"]


def test_services_report_down_and_unconfigured(ctx, server):
    ctx.settings.mangadex_api = server.url + "/md"
    server.routes["/md/ping"] = (200, "text/plain", b"pong")
    ctx.settings.jikan_api = "http://127.0.0.1:1"          # port fermé : injoignable
    items = {i["label"].split(" ")[0]: i for i in health.services(ctx)}
    assert items["MangaDex"]["level"] == "ok" and items["MyAnimeList"]["level"] == "bad"
    assert items["Reddit"]["level"] == "warn"


def test_backup_rotation_and_export(ctx, client):
    ctx.repo.add_manga("Un_Manga", "pattern", {})
    for _ in range(9):
        health.backup_now(ctx)
        time.sleep(1.05)                                    # noms à la seconde
    names = health.list_backups(ctx)
    assert len(names) == health.KEEP_BACKUPS
    snap = sqlite3.connect(names[0]["path"])
    assert snap.execute("SELECT name FROM mangas").fetchone()[0] == "Un_Manga"
    snap.close()
    r = client.get("/backup/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "mangarr.db" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert client.post("/backup/now").status_code == 303


def test_library_scan_finds_problems_and_cleans_parts(ctx, server):
    mid = add(ctx, server, "Serie", tomes_enabled=False)
    folder = os.path.join(ctx.settings.chapters_dir, "Serie")
    make_cbz(f"{folder}/Serie_1.cbz")
    make_cbz(f"{folder}/Serie_4.cbz")
    make_cbz(f"{folder}/Serie_5.cbz", corrupt=True)
    make_cbz(f"{folder}/Serie_6.cbz", pages=0)
    with open(f"{folder}/Serie_7.cbz.part", "wb") as f:
        f.write(b"x")
    tomes = os.path.join(ctx.settings.library_dir, "Serie")
    make_cbz(f"{tomes}/Serie_T01.cbz")
    make_cbz(f"{tomes}/Serie_T03.cbz")
    res = health.scan_library(ctx)
    m = res["mangas"][0]
    assert m["id"] == mid and m["gaps_ch"] == [2, 3] and m["gaps_tomes"] == [2]
    assert {c["file"] for c in m["corrupt"]} == {"Serie_5.cbz", "Serie_6.cbz"}
    assert len(m["parts"]) == 1 and res["totals"] == {"corrupt": 2, "gaps": 3, "parts": 1}
    health.STATE["result"] = res
    assert health.clean_parts(ctx) == 1 and not os.path.exists(f"{folder}/Serie_7.cbz.part")
    # hors bibliothèque : jamais supprimé
    health.STATE["result"] = {"mangas": [{"parts": [os.path.join(os.path.dirname(ctx.settings.library_dir), "x.part")]}],
                              "totals": {"parts": 1}}
    assert health.clean_parts(ctx) == 0


def test_scan_async_and_page(ctx, server, client):
    add(ctx, server, "Serie", tomes_enabled=False)
    assert client.post("/quality/scan").status_code == 303
    for _ in range(100):
        if not health.STATE["running"] and health.STATE["result"] is not None:
            break
        time.sleep(0.05)
    assert "Rien à signaler" in client.get("/quality").text


def test_forecast_estimates_next_release(ctx):
    mid = ctx.repo.add_manga("Rythme", "lelscanfr", {})
    ctx.repo.add_manga("Peu", "lelscanfr", {})
    now = time.time()
    for i, days in enumerate((28, 21, 14, 7)):
        ctx.repo.db.execute("INSERT INTO history(manga_id, ts, kind, number) VALUES (?,?,?,?)",
                            (mid, now - days * 86400, "chapter", 100 + i))
    rows = health.forecast(ctx, now)
    assert len(rows) == 1 and rows[0]["name"] == "Rythme"
    assert abs(rows[0]["pace"] - 7 * 86400) < 5 and abs(rows[0]["next"] - now) < 5
    days = health.activity(ctx, now=now)
    assert days and days[0][1][0]["name"] == "Rythme"
    assert "Rythme" in TestClient(create_app(ctx, password="")).get("/calendar").text
