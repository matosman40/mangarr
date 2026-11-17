import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mangarr.core.config import Settings
from mangarr.core.engine import Context


def jpeg(tag):
    return b"\xff\xd8\xff\xe0" + str(tag).encode() + b"\x00" * 20


class FakeServer:
    """Faux serveur : routes = {chemin: (statut, content-type, octets)}."""

    def __init__(self):
        self.routes = {}
        self.hits = []
        self.posts = []     # (chemin, en-têtes, corps) des requêtes POST reçues
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                outer.hits.append(self.path)
                route = outer.routes.get(path, (404, "text/plain", b"not found"))
                status, ctype, body = route(self.path) if callable(route) else route
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts.append((self.path, dict(self.headers), body))
                path = self.path.split("?")[0]
                status, ctype, out = outer.routes.get(("POST", path), (200, "application/json", b"{}"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add_chapter(self, chap, pages, base="/img", fmt="{:02}", start=0, ext=".jpg"):
        for p in range(pages):
            self.routes[f"{base}/{chap}/{fmt.format(p + start)}{ext}"] = (200, "image/jpeg", jpeg(f"{chap}-{p}"))

    def add_json(self, path, obj):
        self.routes[path] = (200, "application/json", json.dumps(obj).encode())

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(library_dir=str(tmp_path / "library"), config_dir=str(tmp_path / "config"),
                    page_delay=0, chapter_delay=0)


@pytest.fixture
def ctx(settings):
    c = Context(settings, echo=None)
    c.repo.set_setting("discover_only_sources", False)     # les tests « Découvrir » historiques lisent tout MangaDex
    return c


@pytest.fixture(autouse=True)
def isolated_source_registry():
    """Le registre des sources est global : on le restaure après chaque test."""
    from mangarr.sources import SOURCES
    saved = dict(SOURCES)
    yield
    SOURCES.clear()
    SOURCES.update(saved)


@pytest.fixture(autouse=True)
def clear_mangadex_cache():
    from mangarr.tomes import mangadex
    mangadex.CACHE.clear()
    yield
    mangadex.CACHE.clear()


@pytest.fixture(autouse=True)
def reset_translation_state():
    from mangarr import translate
    from mangarr.web import discover
    translate.reset_block()
    discover.SYN_CACHE.clear()
    yield
    translate.reset_block()
    discover.SYN_CACHE.clear()


@pytest.fixture(autouse=True)
def no_external_cover_download(monkeypatch):
    """store_md_cover viserait uploads.mangadex.org : on l'enregistre sans rien télécharger."""
    from mangarr.tomes import covers
    calls = []
    monkeypatch.setattr(covers, "store_md_cover", lambda ctx, mid, url: calls.append((mid, url)) or False)
    return calls


@pytest.fixture(autouse=True)
def no_background_work(monkeypatch):
    """Aucun fil d'arrière-plan pendant les tests : les appels sont simplement enregistrés."""
    from mangarr import catalog
    from mangarr.tomes import covers
    calls = {"catalog": [], "covers": []}
    monkeypatch.setattr(catalog, "refresh_async", lambda ctx, only=None, stale_only=False: calls["catalog"].append(only) or True)
    monkeypatch.setattr(covers, "ensure_async", lambda ctx, force=False: calls["covers"].append(force) or True)
    monkeypatch.setattr(catalog, "PAUSE", 0)
    return calls
