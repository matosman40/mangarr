import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from mangarr import autoadd, library_scan
from mangarr.sources import SOURCES
from mangarr.web.app import create_app
from tests.test_browse import catalogue
from tests.test_discover import MID, entry

AGG = {"volumes": {str(v): {"chapters": {str(c): {} for c in range(9 * v - 8, 9 * v + 1)}} for v in range(1, 5)}}


@pytest.fixture
def client(ctx, server, monkeypatch):
    ctx.settings.mangadex_api = server.url + "/md"
    monkeypatch.setattr(SOURCES["lelscanfr"], "defaults", {
        **SOURCES["lelscanfr"].defaults, "search_url": server.url + "/manga?title={q}&page={page}"})
    server.add_json(f"/md/manga/{MID}", {"data": entry(1, "One Piece")})
    server.add_json("/md/manga", {"data": [entry(1, "One Piece")], "total": 1})
    catalogue(server, [("one-piece", "One Piece"), ("one-piece-party", "One Piece Party")])
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def follow(client, md_id=MID):
    return client.post("/discover/follow", data={"id": md_id}, headers={"Accept": "application/json"})


def shelf(ctx, name, files, base=None):
    d = os.path.join(base or ctx.settings.library_dir, name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        with zipfile.ZipFile(os.path.join(d, f), "w") as z:
            z.writestr("001.jpg", b"x")
    return d


# ------------------------------------------------------------------ nouveau manga
def test_follow_finds_source_automatically(client, ctx, server):
    r = follow(client)
    res = r.json()
    assert r.status_code == 200 and res["created"] and res["found"] and res["source"] == "lelscanfr"
    assert res["slug"] == "one-piece" and "Ajouté" in res["message"] and res["url"] == f"/manga/{res['id']}"
    m = ctx.repo.get_manga(res["id"])
    assert m["enabled"] and m["start_chap"] == 1 and m["params"] == {
        "mangadex_id": MID, "mangadex_title": "One Piece", "slug": "one-piece"}
    assert client.get(res["url"]).status_code == 200


def test_follow_twice_does_not_duplicate(client, ctx):
    first = follow(client).json()
    second = follow(client).json()
    assert not second["created"] and second["id"] == first["id"] and "Déjà suivi" in second["message"]
    assert len(ctx.repo.list_mangas()) == 1


def test_name_collision_gets_suffix(client, ctx):
    ctx.repo.add_manga("One_Piece", "pattern", {"mangadex_id": "autre"})
    assert follow(client).json()["name"] == "One_Piece_2"


def test_no_site_found_creates_manga_to_configure(client, ctx, server):
    catalogue(server, [("naruto", "Naruto")])
    res = follow(client).json()
    assert res["created"] and not res["found"] and not res["enabled"] and "Modifier" in res["message"]
    m = ctx.repo.get_manga(res["id"])
    assert not m["enabled"] and ctx.repo.get_state(m["id"])["last_message"] == autoadd.SETUP_MESSAGE
    assert "à configurer" in client.get("/grid").text and "terminer la configuration" in client.get(res["url"]).text
    assert 'name="enabled" checked' in client.get(f"{res['url']}/edit").text       # préchoisi pour finir la config


def test_similar_but_different_title_is_rejected(client, ctx, server):
    catalogue(server, [("one-piece-party", "One Piece Party")])
    assert not follow(client).json()["found"]


def test_alternative_titles_are_used(client, ctx, server):
    e = entry(1, "Ao Lock")
    e["attributes"]["altTitles"] = [{"en": "Blue Lock"}]
    server.add_json(f"/md/manga/{MID}", {"data": e})
    catalogue(server, [("blue-lock", "Blue Lock")])
    assert follow(client).json()["slug"] == "blue-lock"


def test_start_at_latest_chapter_option(client, ctx):
    assert client.post("/settings/autoadd", data={"start": "latest"}).status_code == 303
    assert ctx.repo.get_manga(follow(client).json()["id"])["start_chap"] == 1100
    ctx.repo.set_setting("auto_add", {"start": "hack"})
    client.post("/settings/autoadd", data={"start": "hack"})
    assert ctx.repo.get_setting("auto_add") == {"start": "first"}


def test_cover_is_requested_from_musicdex_host_only(client, ctx, no_external_cover_download):
    follow(client)
    (mid, url), = no_external_cover_download
    assert url.startswith("https://uploads.mangadex.org/covers/")


def test_follow_endpoint_errors(client, server):
    assert client.post("/discover/follow", data={"id": "../x"}).status_code == 400
    server.routes[f"/md/manga/{MID}"] = (404, "text/plain", b"x")
    r = follow(client)
    assert r.status_code == 502 and "injoignable" in r.json()["message"]


def test_follow_without_json_redirects(client):
    r = client.post("/discover/follow", data={"id": MID})
    assert r.status_code == 303 and r.headers["location"].startswith("/manga/")


# ------------------------------------------------------------------ dossiers déjà présents
def test_volume_number_parsing():
    cases = {"One_Piece_T01.cbz": 1, "One Piece - Tome 12.cbz": 12, "Vol. 03.cbz": 3, "Naruto v07.cbz": 7,
             "Dragon_Ball_T2 - Tome 05.cbz": 5, "cover.jpg": None, "Title2.cbz": None, "Berserk Volume 41.pdf": 41,
             "Notes.txt": None}
    for name, vol in cases.items():
        assert library_scan.volume_number(name) == vol, name


def test_scan_library(ctx):
    shelf(ctx, "One_Piece", ["One_Piece_T01.cbz", "One_Piece_T02.cbz", "cover.jpg.txt"])
    shelf(ctx, ".hidden", ["x_T01.cbz"])
    shelf(ctx, "Vide", [])
    shelf(ctx, "Blue_Lock", [f"Blue_Lock_{c}.cbz" for c in (1, 2, 3)], base=ctx.settings.chapters_dir)
    ctx.repo.add_manga("One_Piece", "pattern", {})
    found = {s["name"]: s for s in library_scan.scan(ctx)}
    assert set(found) == {"One_Piece", "Blue_Lock"}                     # _chapters, caché et vide ignorés
    assert found["One_Piece"]["tomes"] == [1, 2] and found["One_Piece"]["tracked"]
    assert found["Blue_Lock"]["chapters"] == [1, 2, 3] and not found["Blue_Lock"]["tracked"]


def test_existing_tomes_resume_after_last_owned_chapter(client, ctx, server):
    shelf(ctx, "One_Piece", ["One_Piece_T01.cbz", "One_Piece_T02.cbz", "One_Piece_T03.cbz"])
    server.add_json(f"/md/manga/{MID}/aggregate", AGG)
    res = follow(client).json()
    assert res["name"] == "One_Piece" and res["local"] and res["resume"] == 27 and res["enabled"]
    m = ctx.repo.get_manga(res["id"])
    assert ctx.repo.get_state(m["id"])["last_chapter"] == 27 and "chapitre 27" in res["message"]
    assert m["tomes_folder"] is None and os.path.isdir(os.path.join(ctx.settings.library_dir, m["name"]))


def test_existing_folder_with_unknown_resume_stays_suspended(client, ctx, server):
    shelf(ctx, "One_Piece", ["One_Piece_T01.cbz"])
    res = follow(client).json()                           # pas de données de volumes : impossible de situer le tome
    m = ctx.repo.get_manga(res["id"])
    assert res["local"] and res["resume"] is None and not m["enabled"]
    assert ctx.repo.get_state(m["id"])["last_message"] == autoadd.EXISTING_MESSAGE
    assert ctx.repo.get_state(m["id"])["last_chapter"] is None             # rien ne sera retéléchargé


def test_existing_chapters_resume_without_mangadex_data(client, ctx):
    shelf(ctx, "One_Piece", [f"One_Piece_{c}.cbz" for c in range(1, 6)], base=ctx.settings.chapters_dir)
    res = follow(client).json()
    assert res["resume"] == 5 and ctx.repo.get_state(res["id"])["last_chapter"] == 5


def test_discover_badge_for_owned_manga(client, ctx, server):
    server.add_json("/md/manga/tag", {"data": []})
    shelf(ctx, "One_Piece", ["One_Piece_T01.cbz"])
    t = client.get("/discover/results").text
    assert "en bibliothèque" in t and "Suivre (déjà en bibliothèque)" in t


# ------------------------------------------------------------------ page Bibliothèque
def test_library_page_and_adopt(client, ctx, server):
    shelf(ctx, "One_Piece", ["One_Piece_T01.cbz"])
    server.add_json(f"/md/manga/{MID}/aggregate", AGG)
    server.add_json("/md/manga", {"data": [entry(1, "One Piece")], "total": 1})
    page = client.get("/library").text
    assert "One Piece" in page and "Tout suivre (1)" in page and "T1" in page
    r = client.post("/library/adopt", data={"name": "One_Piece"})
    assert r.status_code == 303
    m = ctx.repo.get_manga("One_Piece")
    assert m and m["source"] == "lelscanfr" and m["params"]["mangadex_id"] == "00000001-2222-3333-4444-555555555555"
    assert client.post("/library/adopt", data={"name": "One_Piece"}).status_code == 404       # déjà suivi
    assert "suivi" in client.get("/library").text and "Tout suivre" not in client.get("/library").text


def test_adopt_unknown_to_mangadex_keeps_folder_name(client, ctx, server):
    shelf(ctx, "Titre Obscur", ["Titre Obscur_T01.cbz"])
    assert client.post("/library/adopt", data={"name": "Titre Obscur"}).status_code == 303
    m = ctx.repo.get_manga("Titre Obscur")
    assert m and "mangadex_id" not in m["params"] and not m["enabled"]


def test_adopt_all(client, ctx):
    for n in ("Aaa Un", "Bbb Deux"):
        shelf(ctx, n, [f"{n}_T01.cbz"])
    r = client.post("/library/adopt-all")
    assert r.status_code == 303 and "done=2" in r.headers["location"]
    assert {m["name"] for m in ctx.repo.list_mangas()} == {"Aaa Un", "Bbb Deux"}


def test_new_routes_need_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    assert c.get("/library").status_code == 303 and c.post("/library/adopt-all").status_code == 303
    assert c.post("/discover/follow", data={"id": MID}).status_code == 303 and c.post("/settings/autoadd", data={}).status_code == 303
