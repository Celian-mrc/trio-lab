// Tri multi-colonnes façon tableur (Excel/Google Sheets) sur les tier lists.
// Clic simple sur un en-tête : géré par le lien lui-même (trie par cette
// seule colonne, cf. sort_link dans tierlist.html/duos.html). Maj-clic :
// ajoute (ou, si déjà présente, inverse) cette colonne comme critère de tri
// suivant, sans perdre les critères déjà choisis.
//
// preventDefault + stopPropagation, en capture phase : on coupe l'événement
// avant qu'htmx (délégué en bubble phase sur <body hx-boost>) ne le voie —
// simplement réécrire le href ne suffisait pas (htmx semblait partir sur son
// URL d'origine, avant la réécriture). On navigue nous-mêmes en plein
// rechargement pour ce cas précis : pas de boost AJAX sur le Maj-clic, mais
// un comportement garanti, indépendant des détails internes d'htmx.
document.addEventListener(
  "click",
  function (event) {
    if (!event.shiftKey) return;
    var link = event.target.closest("a[data-sort-key]");
    if (!link) return;
    event.preventDefault();
    event.stopPropagation();

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
    window.location.href = link.pathname + "?" + params.toString();
  },
  true
);
