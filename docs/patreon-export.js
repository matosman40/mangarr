// Export des posts Patreon d'une page (collection, liste de posts ou post seul) pour Mangarr.
//
// Mode d'emploi :
//   1. Connectez-vous à Patreon dans votre navigateur et ouvrez la collection (ou le post).
//      Faites défiler pour que tous les posts voulus soient affichés.
//   2. Ouvrez la console (F12 > Console), collez ce script, validez avec Entrée.
//   3. Le navigateur télécharge un fichier <id_du_post>.json par post
//      (autorisez les téléchargements multiples si le navigateur le demande).
//   4. Déposez ces fichiers sur la page « Patreon » de Mangarr.
//
// Ce script utilise votre session déjà ouverte dans le navigateur. Il n'extrait, n'affiche
// et ne stocke aucun cookie ni jeton. Les liens d'images contenus dans les exports expirent
// (environ 38 h / 14 jours) : déposez les fichiers rapidement.
(async () => {
  const idOf = (u) => (String(u).match(/(\d{6,})(?:[/?#]|$)/) || [])[1];
  const ids = new Set();
  if (/\/posts\//.test(location.pathname)) ids.add(idOf(location.href));
  document.querySelectorAll('a[href*="/posts/"]').forEach((a) => ids.add(idOf(a.href)));
  ids.delete(undefined);
  if (!ids.size) { console.warn("Aucun post trouvé sur cette page."); return; }

  const api = (id) => `https://www.patreon.com/api/posts/${id}?include=images,media`
    + "&fields[post]=title,post_metadata,post_type"
    + "&fields[media]=download_url,file_name,image_urls,mimetype&json-api-version=1.0";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let ok = 0;
  console.log(`${ids.size} post(s) à exporter…`);
  for (const id of ids) {
    try {
      const r = await fetch(api(id), { credentials: "include", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const blob = new Blob([JSON.stringify(data)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${id}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      ok++;
      console.log(`✅ ${id}`);
    } catch (e) {
      console.error(`❌ ${id} : ${e.message}`);
    }
    await sleep(1500);   // on reste poli avec le serveur
  }
  console.log(`Terminé : ${ok}/${ids.size} export(s). Déposez-les dans Mangarr (page Patreon).`);
})();
