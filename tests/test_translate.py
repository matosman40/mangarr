import os

import pytest
from fastapi.testclient import TestClient

from mangarr import translate
from mangarr.tomes.mangadex import clean_synopsis
from mangarr.web import discover
from mangarr.web.app import create_app
from tests.test_discover import MID, entry

MM_OK = {"responseStatus": 200, "responseData": {"translatedText": "Bonjour l&#39;ami"}}
QUOTA = {"responseStatus": 429, "quotaFinished": True, "responseData": {"translatedText": "MYMEMORY WARNING: x"}}
ENGLISH = "The strongest hero returns. He must save everyone! Nobody believes him."


def use_mymemory(ctx, server):
    ctx.repo.set_setting("translation", {"provider": "mymemory", "mymemory_url": server.url + "/mm"})


def use_libre(ctx, server, key=""):
    ctx.repo.set_setting("translation", {"provider": "libretranslate", "url": server.url, "api_key": key})
    server.routes[("POST", "/translate")] = (200, "application/json", b'{"translatedText": "Salut le monde"}')


# ------------------------------------------------------------------ moteur
def test_chunks_respect_limit_and_keep_text():
    text = " ".join(f"Phrase numéro {i} qui est assez longue pour remplir." for i in range(40))
    parts = translate.chunks(text)
    assert all(len(p) <= translate.CHUNK for p in parts) and len(parts) > 3
    assert " ".join(parts).split() == text.split()
    assert translate.chunks("x" * 1200) and all(len(p) <= translate.CHUNK for p in translate.chunks("x" * 1200))


def test_mymemory_translates_caches_and_unescapes(ctx, server):
    use_mymemory(ctx, server)
    server.add_json("/mm", MM_OK)
    out, err = translate.translate(ctx, ENGLISH, "en-US")
    assert (out, err) == ("Bonjour l'ami", None)
    assert "langpair=en%7Cfr" in server.hits[-1]
    n = len(server.hits)
    assert translate.translate(ctx, ENGLISH, "en")[0] == "Bonjour l'ami"      # servi par le cache disque
    assert len(server.hits) == n


def test_mymemory_long_text_is_chunked(ctx, server):
    use_mymemory(ctx, server)
    server.add_json("/mm", {"responseStatus": 200, "responseData": {"translatedText": "Un"}})
    text = " ".join(f"Sentence number {i} is rather long indeed." for i in range(40))
    out, _ = translate.translate(ctx, text, "en")
    assert out.split() == ["Un"] * len([h for h in server.hits if h.startswith("/mm")])
    assert len(out.split()) > 3


def test_mymemory_quota_blocks_further_calls(ctx, server):
    use_mymemory(ctx, server)
    server.add_json("/mm", QUOTA)
    out, err = translate.translate(ctx, ENGLISH, "en")
    assert out is None and "quota" in err
    n = len(server.hits)
    assert "quota" in translate.translate(ctx, "Another text here.", "en")[1]
    assert len(server.hits) == n                                          # plus aucun appel pendant le blocage


def test_libretranslate_and_key(ctx, server):
    use_libre(ctx, server, key="SECRETKEY")
    assert translate.translate(ctx, ENGLISH, "en") == ("Salut le monde", None)
    assert b'"api_key": "SECRETKEY"' in server.posts[-1][2] and b'"target": "fr"' in server.posts[-1][2]


def test_errors_never_leak_secrets(ctx):
    ctx.repo.set_setting("translation", {"provider": "libretranslate", "url": "http://127.0.0.1:1", "api_key": "SECRETKEY"})
    out, err = translate.translate(ctx, ENGLISH, "en")
    assert out is None and "SECRETKEY" not in err and "127.0.0.1" not in err
    ctx.repo.set_setting("translation", {"provider": "libretranslate", "url": "ftp://x"})
    assert "invalide" in translate.translate(ctx, "Text to translate.", "en")[1]


def test_disabled_and_empty(ctx):
    ctx.repo.set_setting("translation", {"provider": "none"})
    assert translate.translate(ctx, ENGLISH, "en") == (None, "traduction désactivée")
    assert translate.translate(ctx, "  ", "en") == ("", None)


def test_clean_synopsis():
    raw = "A **great** story with [a link](http://x.y) inside. " * 2 + "\n\n---\n**Links:**\n- [MAL](http://mal)"
    assert clean_synopsis(raw) == "A great story with a link inside. A great story with a link inside."
    assert clean_synopsis("Short\n---\nafter") == "Short\n\nafter"      # trop court : on ne coupe pas
    assert clean_synopsis("") == ""


# ------------------------------------------------------------------ pages
@pytest.fixture
def client(ctx, server):
    ctx.settings.mangadex_api = server.url + "/md"
    server.add_json("/md/manga/tag", {"data": []})
    server.add_json("/md/manga", {"data": [entry(1, desc={"en": ENGLISH}), entry(2, "Un Titre", desc={"fr": "Déjà en français."})],
                                  "total": 2})
    return TestClient(create_app(ctx, password=""), follow_redirects=False)


def test_only_non_french_cards_get_translation_placeholder(client, ctx, server):
    use_libre(ctx, server)
    t = client.get("/discover/results").text
    assert t.count("/discover/summary?id=") == 1 and "traduction en cours" in t and "Déjà en français." in t
    ctx.repo.set_setting("translation", {"provider": "none"})
    assert "/discover/summary" not in client.get("/discover/results?q=x").text


def test_summary_route_translates(client, ctx, server):
    use_libre(ctx, server)
    client.get("/discover/results")
    r = client.get("/discover/summary?id=00000001-2222-3333-4444-555555555555")
    assert r.status_code == 200 and "Salut le monde" in r.text and "non traduit" not in r.text
    assert client.get("/discover/summary?id=pas-un-uuid").status_code == 400


def test_summary_fetches_when_cache_empty_and_falls_back_on_error(client, ctx, server):
    use_libre(ctx, server)
    server.add_json(f"/md/manga/{MID}", {"data": entry(1, desc={"en": ENGLISH})})
    assert "Salut le monde" in client.get(f"/discover/summary?id={MID}").text      # redémarrage : cache vide
    server.routes[("POST", "/translate")] = (500, "application/json", b'{"error": "panne"}')
    discover.SYN_CACHE.clear()
    server.add_json("/md/manga/22222222-2222-3333-4444-555555555555", {"data": entry(2, desc={"en": "Original text stays."})})
    r = client.get("/discover/summary?id=22222222-2222-3333-4444-555555555555")
    assert "Original text stays." in r.text and "non traduit" in r.text and "panne" in r.text


def test_translation_settings(client, ctx, server):
    assert client.get("/settings").status_code == 200
    r = client.post("/settings/translation", data={"provider": "libretranslate", "tr_url": server.url, "tr_key": "TOPSECRET"})
    assert r.status_code == 303
    assert "TOPSECRET" not in client.get("/settings").text
    client.post("/settings/translation", data={"provider": "libretranslate", "tr_url": server.url})
    assert ctx.repo.get_setting("translation")["api_key"] == "TOPSECRET"          # conservée si champ vide
    client.post("/settings/translation", data={"provider": "libretranslate", "tr_url": server.url, "clear_tr_key": "on"})
    assert "api_key" not in ctx.repo.get_setting("translation")
    client.post("/settings/translation", data={"provider": "hack", "tr_url": "javascript:x"})
    assert ctx.repo.get_setting("translation")["provider"] == "libretranslate"      # valeur invalide refusée
    server.routes[("POST", "/translate")] = (200, "application/json", "{\"translatedText\": \"Le héros\"}".encode())
    assert "Le héros" in client.post("/settings/translation/test").text


# ------------------------------------------------------------------ proxy des jaquettes
COVER_PATH = f"/covers/{MID}/c.jpg.512.jpg"


@pytest.fixture
def proxy(client, server, monkeypatch):
    monkeypatch.setattr(discover, "ALLOWED_HOSTS", {server.url.replace("http://", "")})
    monkeypatch.setattr(discover, "ALLOWED_SCHEMES", {"http"})
    server.routes[COVER_PATH] = (200, "image/jpeg", b"\xff\xd8\xff\xe0" + b"c" * 50)
    return client


def test_cover_proxy_serves_and_caches(proxy, server):
    url = server.url + COVER_PATH
    r = proxy.get("/proxy/cover", params={"u": url})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "max-age" in r.headers["cache-control"]
    n = len(server.hits)
    assert proxy.get("/proxy/cover", params={"u": url}).status_code == 200
    assert len(server.hits) == n                                                  # 2e fois : cache disque


def test_cover_proxy_refuses_everything_else(proxy, server):
    ok = server.url + COVER_PATH
    for bad in ["http://evil.example" + COVER_PATH, "https://uploads.mangadex.org" + COVER_PATH, ok + "?x=1",
                server.url + "/etc/passwd", server.url + f"/covers/{MID}/../secret", "file:///etc/passwd", "", "javascript:1"]:
        assert proxy.get("/proxy/cover", params={"u": bad}).status_code == 400, bad
    assert not any("secret" in h or "passwd" in h for h in server.hits)


def test_cover_proxy_upstream_errors(proxy, server):
    server.routes[f"/covers/{MID}/missing.jpg"] = (404, "text/plain", b"nope")
    assert proxy.get("/proxy/cover", params={"u": server.url + f"/covers/{MID}/missing.jpg"}).status_code == 502
    server.routes[f"/covers/{MID}/html.jpg"] = (200, "text/html", b"<html>captcha</html>")
    r = proxy.get("/proxy/cover", params={"u": server.url + f"/covers/{MID}/html.jpg"})
    assert r.status_code == 502 and "non-image" in r.text


def test_proxy_and_summary_need_auth(ctx):
    c = TestClient(create_app(ctx, password="pw"), follow_redirects=False)
    assert c.get("/proxy/cover?u=x").status_code == 303
    assert c.get(f"/discover/summary?id={MID}").status_code == 303
    assert c.post("/settings/translation", data={}).status_code == 303


# ------------------------------------------------------------------ genres / thèmes en français, Google + repli
MD_TAGS = ["Action", "Adventure", "Boys' Love", "Comedy", "Crime", "Drama", "Fantasy", "Girls' Love", "Historical",
           "Horror", "Isekai", "Magical Girls", "Mecha", "Medical", "Mystery", "Philosophical", "Psychological",
           "Romance", "Sci-Fi", "Slice of Life", "Sports", "Superhero", "Thriller", "Tragedy", "Wuxia", "Aliens",
           "Animals", "Cooking", "Crossdressing", "Delinquents", "Demons", "Genderswap", "Ghosts", "Gyaru", "Harem",
           "Incest", "Loli", "Mafia", "Magic", "Martial Arts", "Military", "Monster Girls", "Monsters", "Music",
           "Ninja", "Office Workers", "Police", "Post-Apocalyptic", "Reincarnation", "Reverse Harem", "Samurai",
           "School Life", "Shota", "Supernatural", "Survival", "Time Travel", "Traditional Games", "Vampires",
           "Video Games", "Villainess", "Virtual Reality", "Zombies"]


def test_every_mangadex_genre_and_theme_has_a_french_label():
    from mangarr.tomes.mangadex import FR_TAGS
    assert [t for t in MD_TAGS if t not in FR_TAGS] == []
    assert FR_TAGS["Office Workers"] == "Employés de bureau" and FR_TAGS["Delinquents"] == "Délinquants"
    assert FR_TAGS["Sci-Fi"] == "Science-fiction" and FR_TAGS["Drama"] == "Drame"


def test_tag_chips_are_styled():
    css = open(os.path.join(os.path.dirname(discover.__file__), "static", "style.css"), encoding="utf-8").read()
    assert ".tag{" in css and ".tags" in css and ".meta" in css       # régression : genres collés sans espacement


GOOGLE_OK = [[["Bonjour le monde", "Hello world", None, None, 10]], None, "en"]


def use_google(ctx, server, fallback=False):
    ctx.repo.set_setting("translation", {"provider": "google", "google_url": server.url + "/g",
                                         "mymemory_url": server.url + "/mm", "fallback": fallback})


def test_default_provider_is_google_with_fallback(ctx):
    cfg = translate.get_config(ctx.repo)
    assert cfg["provider"] == "google" and cfg["fallback"] is True


def test_google_translates(ctx, server):
    use_google(ctx, server)
    server.add_json("/g", GOOGLE_OK)
    assert translate.translate(ctx, ENGLISH, "ja") == ("Bonjour le monde", None)
    assert "tl=fr" in server.hits[-1] and "sl=ja" in server.hits[-1] and "client=gtx" in server.hits[-1]


def test_fallback_to_mymemory_when_google_fails(ctx, server):
    use_google(ctx, server, fallback=True)
    server.routes["/g"] = (429, "text/plain", b"limite")
    server.add_json("/mm", MM_OK)
    assert translate.translate(ctx, ENGLISH, "en") == ("Bonjour l'ami", None)
    n = len([h for h in server.hits if h.startswith("/g")])
    assert translate.translate(ctx, "Un autre texte à traduire.", "en")[0] == "Bonjour l'ami"
    assert len([h for h in server.hits if h.startswith("/g")]) == n        # Google est mis en pause après le 429


def test_no_fallback_when_disabled_or_private_service(ctx, server):
    use_google(ctx, server, fallback=False)
    server.routes["/g"] = (500, "text/plain", b"x")
    server.add_json("/mm", MM_OK)
    out, err = translate.translate(ctx, ENGLISH, "en")
    assert out is None and "Google" in err and not any(h.startswith("/mm") for h in server.hits)
    ctx.repo.set_setting("translation", {"provider": "libretranslate", "url": server.url, "fallback": True,
                                         "google_url": server.url + "/g"})
    server.routes[("POST", "/translate")] = (400, "application/json", b'{"error": "panne"}')
    server.add_json("/g", GOOGLE_OK)
    before = len(server.hits)
    out, err = translate.translate(ctx, "Texte privé à garder chez soi.", "en")
    assert out is None and "panne" in err
    assert not any(h.startswith("/g") for h in server.hits[before:])           # rien ne sort du réseau


def test_translation_error_script_in_page(client):
    assert "htmx:responseError" in client.get("/discover").text
    assert "traduction indisponible" in client.get("/discover").text
