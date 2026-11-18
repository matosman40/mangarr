"""Routes de la synchro Patreon : userscript à installer dans le navigateur, API protégée par jeton, ajout de posts."""
import json
import threading
import time
from urllib.parse import quote, urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from mangarr import patreon_sync
from mangarr.core.net import FetchError
from mangarr.core.engine import RUN_LOCK, RunOptions, check_all
from mangarr.sources.patreon import API

USERSCRIPT = r"""// ==UserScript==
// @name         Mangarr - Patreon
// @namespace    mangarr
// @version      1.0
// @description  Envoie à Mangarr les exports JSON et les derniers posts de vos créateurs Patreon (depuis votre navigateur connecté)
// @match        https://www.patreon.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      __HOST__
// @run-at       document-idle
// ==/UserScript==
(function () {
  "use strict";
  var BASE = "__BASE__", TOKEN = "__TOKEN__", EVERY = 3 * 3600 * 1000, PAUSE = 1500;
  var ok = function (u) { return typeof u === "string" && u.indexOf("https://www.patreon.com/api/") === 0; };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function mangarr(method, path, body) {
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: method, url: BASE + path, timeout: 60000,
        headers: {"X-Mangarr-Token": TOKEN, "Content-Type": "application/json"},
        data: body ? JSON.stringify(body) : undefined,
        onload: function (r) { try { r.status < 300 ? resolve(JSON.parse(r.responseText)) : reject(new Error("Mangarr HTTP " + r.status)); } catch (e) { reject(e); } },
        onerror: function () { reject(new Error("Mangarr injoignable")); },
        ontimeout: function () { reject(new Error("Mangarr : délai dépassé")); }
      });
    });
  }
  function patreon(url) {            // même origine que la page, avec VOS cookies de navigateur
    return fetch(url, {credentials: "include", headers: {"Accept": "application/json"}}).then(function (r) {
      if (!r.ok) throw new Error("Patreon HTTP " + r.status);
      return r.json();
    });
  }

  var badge = document.createElement("div");
  badge.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:99999;font:13px system-ui;background:#5b5bd6;color:#fff;padding:6px 12px;border-radius:99px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3)";
  badge.textContent = "Mangarr ⟳";
  badge.title = "Synchroniser maintenant avec Mangarr";
  badge.onclick = function () { run(true); };
  document.body.appendChild(badge);
  function say(t) { badge.textContent = "Mangarr : " + t; }

  var running = false;
  async function run(force) {
    if (running) return;
    var last = Number(GM_getValue("last", 0));
    if (!force && Date.now() - last < EVERY) return;
    running = true;
    try {
      say("demande…");
      var w = await mangarr("GET", "/patreon/wanted");
      var exportsOut = {}, nExp = 0;
      for (var i = 0; i < w.posts.length; i++) {
        var url = w.post_api.replace("{id}", w.posts[i]);
        if (!ok(url)) continue;
        say("export " + (i + 1) + "/" + w.posts.length);
        try { exportsOut[w.posts[i]] = await patreon(url); nExp++; } catch (e) { console.warn("Mangarr", e); }
        await sleep(PAUSE);
      }
      var r = {campaigns: w.campaigns};
      if (nExp) { say("envoi…"); r = await mangarr("POST", "/patreon/ingest", {exports: exportsOut}); }
      var camps = Array.from(new Set((w.campaigns || []).concat(r.campaigns || []))), feeds = {};
      for (var j = 0; j < camps.length; j++) {
        var cu = w.campaign_api.replace("{id}", camps[j]);
        if (!ok(cu)) continue;
        say("posts " + (j + 1) + "/" + camps.length);
        try {
          var d = await patreon(cu);
          feeds[camps[j]] = (d.data || []).map(function (p) {
            var a = p.attributes || {};
            return {id: p.id, title: a.title, date: a.published_at, url: a.url};
          });
        } catch (e) { console.warn("Mangarr", e); }
        await sleep(PAUSE);
      }
      if (Object.keys(feeds).length) await mangarr("POST", "/patreon/ingest", {feeds: feeds});
      GM_setValue("last", Date.now());
      say(nExp + " export(s), " + Object.keys(feeds).length + " créateur(s) ✓");
    } catch (e) {
      say("erreur — " + e.message);
    } finally { running = false; }
  }
  setTimeout(function () { run(false); }, 4000);
})();
"""


def register(app, ctx, render):
    repo = ctx.repo

    def authorized(request):
        return patreon_sync.token_ok(repo, request.headers.get("x-mangarr-token", ""))

    @app.get("/patreon/userscript.user.js")
    def userscript(request: Request):
        """Script à installer (Tampermonkey/Violentmonkey) : contient l'adresse de Mangarr et le jeton."""
        base = str(request.base_url).rstrip("/")
        js = (USERSCRIPT.replace("__BASE__", base).replace("__HOST__", urlparse(base).hostname or "")
              .replace("__TOKEN__", patreon_sync.get_token(repo)))
        return Response(js, media_type="text/javascript; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="mangarr-patreon.user.js"',
                                 "Cache-Control": "no-store"})

    @app.post("/patreon/token/renew")
    def renew_token():
        patreon_sync.get_token(repo, renew=True)
        return RedirectResponse("/patreon", status_code=303)

    # -- appelées par le userscript : pas de cookie de session, jeton obligatoire (voir PUBLIC dans app.py)
    @app.get("/patreon/wanted")
    async def wanted(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "jeton invalide"}, status_code=401)
        data = await run_in_threadpool(patreon_sync.wanted, ctx)
        data["post_api"] = API
        patreon_sync.touch(ctx, seen=time.time())
        return data

    @app.post("/patreon/ingest")
    async def ingest(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "jeton invalide"}, status_code=401)
        raw = await request.body()
        if len(raw) > patreon_sync.MAX_BODY:
            return JSONResponse({"error": "envoi trop gros"}, status_code=413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "JSON invalide"}, status_code=400)
        res = await run_in_threadpool(patreon_sync.ingest, ctx, payload)
        if res["mangas"] and not RUN_LOCK.locked():      # nouveaux exports : on télécharge tout de suite
            threading.Thread(target=lambda: check_all(ctx, only=res["mangas"], opts=RunOptions()),
                             daemon=True, name="patreon-download").start()
        return res

    # -- depuis l'interface (session normale)
    @app.post("/patreon/add-post")
    async def add_post(request: Request):
        form = await request.form()
        try:
            num = await run_in_threadpool(patreon_sync.add_post, ctx, int(form.get("manga") or 0),
                                          (form.get("url") or "").strip())
            msg = f"Post ajouté comme tome {num}"
        except (ValueError, FetchError) as e:
            msg = str(e)
        return RedirectResponse(f"/patreon?msg={quote(msg)}", status_code=303)

    def download_async(names):
        if names and not RUN_LOCK.locked():
            threading.Thread(target=lambda: check_all(ctx, only=names, opts=RunOptions()), daemon=True,
                             name="patreon-download").start()
            return True
        return False

    @app.post("/patreon/paste")
    async def paste(request: Request):
        """Colle le contenu d'un ou plusieurs exports JSON : plus simple que d'enregistrer des fichiers."""
        form = await request.form()
        text = (form.get("text") or "").strip()
        try:
            dec, objs, i = json.JSONDecoder(), [], 0
            while i < len(text):
                try:
                    obj, i = dec.raw_decode(text, i)
                except ValueError:
                    raise ValueError("ce texte n'est pas du JSON valide")
                objs += obj if isinstance(obj, list) else [obj]
                while i < len(text) and text[i] in " \r\n,;":
                    i += 1
            if not objs:
                raise ValueError("rien à importer : collez le contenu de la page d'API")
            saved = [patreon_sync.save_export(ctx, o) for o in objs]
            patreon_sync.touch(ctx, last_ingest=time.time(), last_saved=len(saved))
            names = sorted({m["name"] for m, _, _, posts in patreon_sync._series(ctx)
                            for u in posts.values() if patreon_sync._safe_pid(u) in saved})
            msg = f"{len(saved)} export(s) enregistré(s)" + (" — téléchargement lancé" if download_async(names) else "")
        except ValueError as e:
            msg = f"Import impossible : {e}"
        return RedirectResponse(f"/patreon?msg={quote(msg)}", status_code=303)

    @app.post("/patreon/check")
    async def check_patreon():
        """Relit le dossier inbox puis vérifie toutes les séries Patreon (télécharge ce qui est prêt)."""
        saved, errors = await run_in_threadpool(patreon_sync.scan_inbox, ctx)
        names = [m["name"] for m, _, _, _ in patreon_sync._series(ctx)]
        if not names:
            msg = "Aucune série Patreon"
        else:
            msg = (f"Vérification lancée pour {len(names)} série(s)" if download_async(names)
                   else "Une vérification est déjà en cours")
        if saved:
            msg += f" ; {len(saved)} fichier(s) importé(s) depuis inbox"
        if errors:
            msg += " ; erreurs : " + " | ".join(f"{k} : {v}" for k, v in errors.items())
        return RedirectResponse(f"/patreon?msg={quote(msg)}", status_code=303)
