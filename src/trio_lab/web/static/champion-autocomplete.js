// Autocomplétion des champions : `<datalist id="champion-names">` contient
// TOUS les champions (rendu côté serveur, /duos, /tierlist, /draft) — au
// focus natif d'un `<input list="champion-names">`, le navigateur affiche
// l'intégralité de la liste (~170 champions), pas une recherche (retour
// utilisateur 2026-07-27 : "juste la complétion quand l'utilisateur
// commence à écrire").
//
// Le datalist est vidé au premier focus (liste complète mémorisée sur
// l'élément lui-même, `_allOptions`, pour survivre au vidage) puis repeuplé
// UNIQUEMENT avec les correspondances (sous-chaîne, 20 max) au fil de la
// frappe. Délégué sur `document` (pas de binding direct sur les inputs) :
// htmx (hx-boost) remplace le DOM à chaque navigation, un binding pris
// avant un swap ne survivrait pas — même raison que sort.js/thresholds.js.
// `focus` ne bubble pas : écouté en phase de capture.

(function () {
  var MAX_MATCHES = 20;

  function championDatalist(input) {
    var list = input && input.list;
    return list && list.id === "champion-names" ? list : null;
  }

  function syncOptions(input) {
    var datalist = championDatalist(input);
    if (!datalist) return;
    if (!datalist._allOptions) {
      datalist._allOptions = Array.prototype.map.call(datalist.options, function (option) {
        return option.value;
      });
    }
    var query = input.value.trim().toLowerCase();
    datalist.innerHTML = "";
    if (!query) return;
    datalist._allOptions
      .filter(function (name) {
        return name.toLowerCase().indexOf(query) !== -1;
      })
      .slice(0, MAX_MATCHES)
      .forEach(function (name) {
        var option = document.createElement("option");
        option.value = name;
        datalist.appendChild(option);
      });
  }

  document.addEventListener(
    "focus",
    function (event) {
      syncOptions(event.target);
    },
    true
  );
  document.addEventListener("input", function (event) {
    syncOptions(event.target);
  });
})();
