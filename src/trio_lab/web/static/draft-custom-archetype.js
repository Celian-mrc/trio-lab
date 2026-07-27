// "Compose à partir de tes champions" : les 6 champs de poids
// personnalisés (`.draft-compose-weights`) ne sont utiles que si "Personnalisé"
// est choisi dans le <select> archétype (retour utilisateur 2026-07-28).
// L'état initial est déjà correct côté serveur (`hidden` posé selon
// `selected_archetype`) — ce script gère seulement la bascule EN DIRECT
// pendant que l'utilisateur change de sélection, avant de soumettre.
//
// Délégué sur `document` (pas de binding direct sur le <select>) : htmx
// (hx-boost) remplace le DOM à chaque navigation, un binding pris avant un
// swap ne survivrait pas — même raison que sort.js/thresholds.js.

document.addEventListener("change", function (event) {
  var select = event.target.closest('select[name="archetype"]');
  if (!select) return;
  var form = select.closest("form");
  if (!form) return;
  var weights = form.querySelector(".draft-compose-weights");
  if (!weights) return;
  var isCustom = select.value === "custom";
  weights.hidden = !isCustom;
  if (isCustom) {
    var firstInput = weights.querySelector("input");
    if (firstInput) firstInput.focus();
  }
});
