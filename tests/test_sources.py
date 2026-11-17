import json
import os
import time

import pytest

from mangarr.core.net import ChapterMissing, FetchError, make_session
from mangarr.sources import build_source
from tests.conftest import jpeg


def test_pattern_reads_until_404(server, settings):
    server.add_chapter(5, 3)
    src, params = build_source("pattern", {"base_img_url": server.url + "/img"}, settings)
    assert len(list(src.iter_images(make_session(), params, 5))) == 3


def test_pattern_chapter_missing(server, settings):
    src, params = build_source("pattern", {"base_img_url": server.url + "/img"}, settings)
    with pytest.raises(ChapterMissing):
        list(src.iter_images(make_session(), params, 9))


def test_403_is_error_not_missing(server, settings):
    server.routes["/img/1/00.jpg"] = (403, "text/html", b"blocked")
    src, params = build_source("pattern", {"base_img_url": server.url + "/img"}, settings)
    with pytest.raises(FetchError, match="403"):
        list(src.iter_images(make_session(), params, 1))


def test_magic_bytes_not_extension(server, settings):
    server.routes["/img/1/00.jpg"] = (200, "image/jpeg", b"<html>captcha</html>")
    src, params = build_source("pattern", {"base_img_url": server.url + "/img"}, settings)
    with pytest.raises(ChapterMissing, match="non-image"):
        list(src.iter_images(make_session(), params, 1))


def test_html_source_lists_images(server, settings):
    page = '<img src="/u/a.jpg"><img data-src="/u/b.png"><img src="/logo.png">'
    server.routes["/ch/7"] = (200, "text/html", page.encode())
    server.routes["/u/a.jpg"] = (200, "image/jpeg", jpeg("a"))
    server.routes["/u/b.png"] = (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    src, params = build_source("html", {"chapter_url": server.url + "/ch/{chap}", "img_filter": "/u/"}, settings)
    assert src.probe(make_session(), 7) == 2
    assert len(list(src.iter_images(make_session(), params, 7))) == 2


def test_html_identical_images_are_legit(server, settings):
    server.routes["/ch/1"] = (200, "text/html", b'<img src="/u/a.jpg"><img src="/u/b.jpg">')
    for n in "ab":
        server.routes[f"/u/{n}.jpg"] = (200, "image/jpeg", jpeg("same"))
    src, params = build_source("html", {"chapter_url": server.url + "/ch/{chap}"}, settings)
    assert not src.stop_on_duplicate and len(list(src.iter_images(make_session(), params, 1))) == 2


def test_html_missing_image_is_error(server, settings):
    server.routes["/ch/1"] = (200, "text/html", b'<img src="/u/a.jpg">')
    src, params = build_source("html", {"chapter_url": server.url + "/ch/{chap}"}, settings)
    with pytest.raises(FetchError):
        list(src.iter_images(make_session(), params, 1))


def _export(settings, post_id, urls):
    os.makedirs(settings.patreon_dir, exist_ok=True)
    data = {"data": {"attributes": {"post_metadata": {"image_order": ["1", "2"]}}},
            "included": [{"id": str(i + 1), "type": "media", "attributes": {"download_url": u}}
                         for i, u in enumerate(urls)]}
    with open(f"{settings.patreon_dir}/{post_id}.json", "w") as f:
        json.dump(data, f)


POST = "https://www.patreon.com/creator/posts/titre-123456789"


def test_patreon_missing_export_is_explicit(settings):
    src, params = build_source("patreon", {"posts": {"1": POST}}, settings)
    with pytest.raises(FetchError, match="export manquant"):
        list(src.iter_images(make_session(), params, 1))


def test_patreon_reads_export(server, settings):
    server.routes["/a.jpg"] = (200, "image/jpeg", jpeg("a"))
    server.routes["/b.jpg"] = (200, "image/jpeg", jpeg("a"))   # identique : légitime
    _export(settings, "123456789", [server.url + "/a.jpg", server.url + "/b.jpg"])
    src, params = build_source("patreon", {"posts": {"1": POST}}, settings)
    assert src.explicit_chapters(params) == [1]
    assert len(list(src.iter_images(make_session(), params, 1))) == 2


def test_patreon_expired_export(server, settings):
    old = int(time.time()) - 3600
    _export(settings, "123456789", [f"{server.url}/a.jpg?token-time={old}"] * 2)
    src, params = build_source("patreon", {"posts": {"1": POST}}, settings)
    with pytest.raises(FetchError, match="expiré"):
        list(src.iter_images(make_session(), params, 1))


def test_unknown_source(settings):
    with pytest.raises(ValueError):
        build_source("nope", {}, settings)
