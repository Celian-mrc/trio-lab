// Tri multi-colonnes façon tableur (Excel/Google Sheets) sur les tier lists.
// Clic simple sur un en-tête : géré par le lien lui-même (trie par cette
// seule colonne, cf. sort_link dans tierlist.html/duos.html). Maj-clic :
// ajoute (ou, si déjà présente, inverse) cette colonne comme critère de tri
// suivant, sans perdre les critères déjà choisis.
//
// Capture phase (3e argument `true`) : s'exécute avant le handler de clic
// d'htmx (bubble phase), pour que le boost AJAX parte déjà sur l'URL à jour.
document.addEventListener(
  "click",
  function (event) {
    if (!event.shiftKey) return;
    var link = event.target.closest("a[data-sort-key]");
    if (!link) return;

    var MAX_SORT_LEVELS = 4;
    var params = new URLSearchParams(window.location.search);
    var key = link.dataset.sortKey;
    var sorts = (params.get("sort") || "").split(",").filter(Boolean);
    var dirs = (params.get("dir") || "").split(",").filter(Boolean);
    var idx = sorts.indexOf(key);

    if (idx === -1) {
      if (sorts.length >= MAX_SORT_LEVELS) return;
      sorts.push(key);
      dirs.push("desc");
    } else {
      dirs[idx] = dirs[idx] === "desc" ? "asc" : "desc";
    }

    params.set("sort", sorts.join(","));
    params.set("dir", dirs.join(","));
    params.delete("page"); // repartir de la page 1 sur un nouveau tri
    link.setAttribute("href", link.pathname + "?" + params.toString());
  },
  true
);
