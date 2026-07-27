// Boutons 1/2/3 sur "Compositions suggérées" (retour utilisateur
// 2026-07-27) : jusqu'à 3 propositions par archétype, déjà toutes rendues
// côté serveur (`.draft-suggest-body[hidden]` sur celles qui ne sont pas la
// 1ère) — un clic bascule laquelle est visible, sans aller-retour serveur.
//
// Délégué sur `document` (pas de binding direct sur les boutons) : htmx
// (hx-boost) remplace le DOM à chaque navigation, un binding pris avant un
// swap ne survivrait pas — même raison que sort.js/thresholds.js.

document.addEventListener("click", function (event) {
  var tab = event.target.closest(".draft-variant-tab");
  if (!tab) return;
  var card = tab.closest(".draft-suggest-card");
  if (!card) return;
  var index = tab.dataset.variantIndex;

  card.querySelectorAll(".draft-variant-tab").forEach(function (button) {
    button.classList.toggle("active", button === tab);
  });
  card.querySelectorAll(".draft-suggest-body").forEach(function (body) {
    body.hidden = body.dataset.variantIndex !== index;
  });
});
