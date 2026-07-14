// Filtres par seuils : "+ ajouter une colonne" révèle le champ demandé,
// "✕" le vide et le cache — évite de montrer les 13 champs vides d'un coup
// (retour utilisateur, 2026-07-14). Les <label> et <option> existent déjà
// dans le DOM (rendus par _threshold_filters.html), juste masqués via
// [hidden] : pas de création d'élément côté JS, aucun risque de divergence
// avec les libellés/attributs définis côté serveur.
//
// Délégué sur `document` (pas DOMContentLoaded ni binding direct sur les
// éléments) : le DOM est remplacé par htmx (hx-boost) à chaque navigation,
// un binding pris avant un swap ne survivrait pas (même raison que sort.js).

document.addEventListener("change", function (event) {
  var select = event.target.closest("[data-add-threshold]");
  if (!select || !select.value) return;
  var form = select.closest("form");
  var field = form.querySelector('.threshold-field[data-key="' + select.value + '"]');
  if (field) {
    field.hidden = false;
    var input = field.querySelector("input");
    if (input) input.focus();
  }
  var option = select.querySelector('option[value="' + select.value + '"]');
  if (option) option.hidden = true;
  select.value = "";
});

document.addEventListener("click", function (event) {
  var button = event.target.closest(".remove-threshold");
  if (!button) return;
  var form = button.closest("form");
  var key = button.dataset.key;
  var field = form.querySelector('.threshold-field[data-key="' + key + '"]');
  if (field) {
    field.hidden = true;
    var input = field.querySelector("input");
    if (input) input.value = "";
  }
  var select = form.querySelector("[data-add-threshold]");
  var option = select && select.querySelector('option[value="' + key + '"]');
  if (option) option.hidden = false;
});
