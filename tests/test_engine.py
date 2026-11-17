import json
import os
import time
from zipfile import ZipFile

import pytest

from mangarr.core.engine import RunOptions, check_all
from mangarr.sources import SOURCES, load_custom_sources
from mangarr.tomes import plan


def md_volumes(server, volumes):
    server.add_json("/md/manga/MID/aggregate", {"volumes": {
        str(v): {"chapters": {str(c): {} for c in chs}} for v, chs in volumes.items()}})


def add(ctx, server, name="Test_Manga", **extra):
    params = {"base_img_url": server.url + "/img", "mangadex_id": "MID", "mangadex_api": server.url + "/md"}
    return ctx.repo.add_manga(name, "pattern", params, **extra)


def test_download_until_missing_and_state(ctx, server):
    for c in (1, 2, 3):
        server.add_chapter(c, 2)
    mid = add(ctx, server, tomes_enabled=False)
    res = check_all(ctx)[0]
    assert (res.got, res.status) == (3, "missing")
    folder = os.path.join(ctx.settings.chapters_dir, "Test_Manga")
    assert sorted(os.listdir(folder)) == [f"Test_Manga_{i}.cbz" for i in (1, 2, 3)]
    assert ctx.repo.get_state(mid)["last_chapter"] == 3
    with ZipFile(os.path.join(folder, "Test_Manga_1.cbz")) as z:
        assert "ComicInfo.xml" in z.namelist() and len(z.namelist()) == 3
    assert len(ctx.repo.history(mid)) == 3


def test_resume_after_chapters_deleted(ctx, server):
    for c in (1, 2, 3, 4):
        server.add_chapter(c, 1)
    mid = add(ctx, server, tomes_enabled=False)
    ctx.repo.save_last_chapter(mid, 2)    # état conservé alors que les fichiers ont été supprimés
    res = check_all(ctx)[0]
    assert res.got == 2
    assert not os.path.exists(os.path.join(ctx.settings.chapters_dir, "Test_Manga", "Test_Manga_1.cbz"))


def test_tome_built_then_chapters_removed(ctx, server):
    for c in (1, 2, 3):
        server.add_chapter(c, 2)
    md_volumes(server, {1: [1, 2], 2: [3, 4]})
    mid = add(ctx, server)
    check_all(ctx)
    lib = os.path.join(ctx.settings.library_dir, "Test_Manga")
    with ZipFile(os.path.join(lib, "Test_Manga_T01.cbz")) as z:
        assert len(z.namelist()) == 4 + 1 and z.testzip() is None
        assert "<Volume>1</Volume>" in z.read("ComicInfo.xml").decode()
    folder = os.path.join(ctx.settings.chapters_dir, "Test_Manga")
    left = sorted(f for f in os.listdir(folder) if f.endswith(".cbz"))
    assert left == ["Test_Manga_3.cbz"]            # 1-2 supprimés, 3 gardé (tome 2 incomplet)
    assert ctx.repo.get_state(mid)["last_chapter"] == 3
    assert not any(f.endswith(".part") for f in os.listdir(lib))
    assert os.path.isfile(os.path.join(lib, "cover.jpg"))


def test_invalid_tome_keeps_chapters(ctx, server, monkeypatch):
    for c in (1, 2):
        server.add_chapter(c, 2)
    md_volumes(server, {1: [1, 2]})
    add(ctx, server)
    monkeypatch.setattr("mangarr.core.engine.tome_is_valid", lambda *a: False)
    check_all(ctx)
    folder = os.path.join(ctx.settings.chapters_dir, "Test_Manga")
    assert {"Test_Manga_1.cbz", "Test_Manga_2.cbz"} <= set(os.listdir(folder))


def test_build_tome_rejects_inconsistent_page_count(monkeypatch, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    for c in (1, 2):
        with ZipFile(folder / f"X_{c}.cbz", "w") as z:
            z.writestr("001.jpg", b"a")
    real = plan.count_images
    monkeypatch.setattr(plan, "count_images", lambda p: real(p) + 1 if str(p).endswith(".part") else real(p))
    with pytest.raises(ValueError):
        plan.build_tome("X", str(folder), 1, [1, 2], str(tmp_path / "out"))
    assert not os.listdir(tmp_path / "out")      # ni tome ni .part


def test_mangadex_down_uses_stale_cache(ctx, server):
    for c in (1, 2):
        server.add_chapter(c, 1)
    md_volumes(server, {1: [1, 2]})
    add(ctx, server, tomes_enabled=True)
    check_all(ctx, opts=RunOptions(keep_chapters=True))
    cache = ctx.settings.cache_dir
    path = os.path.join(cache, [f for f in os.listdir(cache) if f.endswith(".volumes.json")][0])
    d = json.load(open(path))
    d["fetched"] = time.time() - 99999     # cache périmé
    json.dump(d, open(path, "w"))
    server.routes.pop("/md/manga/MID/aggregate")      # API KO : le cache périmé sert
    os.remove(os.path.join(ctx.settings.library_dir, "Test_Manga", "Test_Manga_T01.cbz"))
    check_all(ctx, opts=RunOptions(tomes_only=True, keep_chapters=True))
    assert os.path.isfile(os.path.join(ctx.settings.library_dir, "Test_Manga", "Test_Manga_T01.cbz"))


def test_error_on_one_manga_does_not_stop_others(ctx, server):
    server.add_chapter(1, 1)
    ctx.repo.add_manga("A_Broken", "pattern", {"base_img_url": server.url + "/nope"}, tomes_enabled=False)
    server.routes["/img403/1/00.jpg"] = (403, "text/html", b"x")
    ctx.repo.add_manga("B_Blocked", "pattern", {"base_img_url": server.url + "/img403"}, tomes_enabled=False)
    add(ctx, server, "C_Good", tomes_enabled=False)
    res = {r.name: r for r in check_all(ctx)}
    assert res["B_Blocked"].status == "error" and "403" in res["B_Blocked"].message
    assert res["A_Broken"].status == "missing"
    assert res["C_Good"].got == 1


def test_patreon_posts_independent(ctx, server, settings):
    posts = {"1": "https://www.patreon.com/c/posts/a-111111111", "2": "https://www.patreon.com/c/posts/b-222222222"}
    ctx.repo.add_manga("Series_P", "patreon", {"posts": posts})
    server.routes["/a.jpg"] = (200, "image/jpeg", b"\xff\xd8\xff" + b"1" * 10)
    os.makedirs(settings.patreon_dir)
    with open(f"{settings.patreon_dir}/222222222.json", "w") as f:
        json.dump({"data": {"attributes": {"post_metadata": {"image_order": ["1"]}}},
                   "included": [{"id": "1", "type": "media",
                                 "attributes": {"download_url": server.url + "/a.jpg"}}]}, f)
    res = check_all(ctx)[0]
    assert res.got == 1 and res.status == "error"     # le post 1 échoue, le 2 passe
    assert os.path.isfile(os.path.join(settings.library_dir, "Series_P", "Series_P_T02.cbz"))


def test_dry_run_writes_nothing(ctx, server):
    server.add_chapter(1, 1)
    add(ctx, server)
    check_all(ctx, opts=RunOptions(dry_run=True))
    assert not os.path.exists(ctx.settings.chapters_dir)


def test_custom_form_source(ctx, server):
    server.add_chapter(1, 2, base="/mine")
    ctx.repo.save_custom_source("monsite", "pattern", {"base_img_url": server.url + "/mine"})
    load_custom_sources(ctx.repo)
    ctx.repo.add_manga("Via_Form", "monsite", {}, tomes_enabled=False)
    assert check_all(ctx)[0].got == 1


def test_plugin_loading(ctx, settings):
    os.makedirs(settings.plugins_dir)
    with open(os.path.join(settings.plugins_dir, "bad.py"), "w") as f:
        f.write("raise RuntimeError('boom')")
    with open(os.path.join(settings.plugins_dir, "good.py"), "w") as f:
        f.write("from mangarr.sources import Source, register\n"
                "@register('plugin_ok')\nclass P(Source):\n"
                "    def iter_images(self, s, p, c):\n        return iter([])\n")
    ctx.reload_sources()
    assert "plugin_ok" in SOURCES and any("bad.py" in e for e in ctx.plugin_errors)
