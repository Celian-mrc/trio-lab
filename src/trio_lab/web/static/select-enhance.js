// Custom Select façon shadcn/ui (retour utilisateur 2026-08-12) : la balise
// <select> native ne peut pas être stylée à l'ouverture, la liste déroulée
// est dessinée par l'OS, pas par le navigateur — le chevron custom sur l'état
// fermé (style.css) ne suffisait pas. Ce script remplace visuellement chaque
// <select> par un déclencheur + une liste custom (`.ts-select`), le <select>
// d'origine restant dans le DOM (`hidden`) pour que la soumission de
// formulaire reste inchangée côté serveur — aucune modification des routes
// ni des templates.
//
// Les options affichées sont reconstruites à CHAQUE ouverture (pas une seule
// fois à l'enrichissement) : le <select data-add-threshold>
// (_threshold_filters.html) voit ses <option> cachées/révélées en direct par
// thresholds.js après le chargement — une liste figée à froid divergerait
// vite. Choisir une option custom fait select.value = ... puis dispatch un
// vrai 'change' natif (bubbles: true) : thresholds.js/draft-custom-
// archetype.js écoutent déjà ce 'change' sur le <select> d'origine, aucun
// changement requis de leur côté.
//
// Interaction (clic, clavier) déléguée sur `document`, comme sort.js/
// thresholds.js/etc. : survit aux swaps htmx sans re-binding. Seule la
// CONSTRUCTION du déclencheur/liste doit se refaire à chaque swap — d'où
// l'écoute de `htmx:load` (déclenché aussi au chargement initial), seul
// script du projet à créer des éléments plutôt que d'écouter des événements
// sur l'existant.

(function () {
  var uid = 0;

  function closestLabelText(select) {
    var label = select.closest("label");
    if (!label) return "";
    var text = "";
    label.childNodes.forEach(function (node) {
      if (node.nodeType === Node.TEXT_NODE) text += node.textContent;
    });
    return text.trim();
  }

  function wrapperOf(select) {
    return select.closest(".ts-select");
  }

  function triggerOf(select) {
    var wrapper = wrapperOf(select);
    return wrapper && wrapper.querySelector(".ts-select-trigger");
  }

  function listboxOf(select) {
    var wrapper = wrapperOf(select);
    return wrapper && wrapper.querySelector(".ts-select-listbox");
  }

  function syncTriggerLabel(select) {
    var trigger = triggerOf(select);
    if (!trigger) return;
    var selected = select.options[select.selectedIndex];
    trigger.querySelector(".ts-select-value").textContent = selected ? selected.textContent : "";
  }

  function buildOptions(select, listbox, listboxId) {
    listbox.innerHTML = "";
    Array.prototype.forEach.call(select.options, function (option, index) {
      if (option.hidden) return;
      var li = document.createElement("li");
      li.className = "ts-select-option";
      li.id = listboxId + "-opt-" + index;
      li.setAttribute("role", "option");
      li.dataset.value = option.value;
      li.textContent = option.textContent;
      var isSelected = option.value === select.value;
      li.setAttribute("aria-selected", isSelected ? "true" : "false");
      if (isSelected) li.classList.add("highlighted");
      listbox.appendChild(li);
    });
  }

  function enhance(select) {
    if (select.dataset.enhanced) return;
    select.dataset.enhanced = "1";

    var wrapper = document.createElement("span");
    wrapper.className = "ts-select";
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);
    select.hidden = true;

    var listboxId = "ts-select-listbox-" + ++uid;

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "ts-select-trigger";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", listboxId);
    // Reprend data-tooltip/aria-label du <select> d'origine s'il y en avait
    // (ex. roles select de duos.html) — sinon dérive du <label> englobant.
    var tooltip = select.getAttribute("data-tooltip");
    if (tooltip) trigger.setAttribute("data-tooltip", tooltip);
    var ariaLabel = select.getAttribute("aria-label") || closestLabelText(select);
    if (ariaLabel) trigger.setAttribute("aria-label", ariaLabel);

    var valueSpan = document.createElement("span");
    valueSpan.className = "ts-select-value";
    trigger.appendChild(valueSpan);

    var chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.setAttribute("class", "ts-select-chevron");
    chevron.setAttribute("viewBox", "0 0 24 24");
    chevron.setAttribute("fill", "none");
    chevron.setAttribute("stroke", "currentColor");
    chevron.setAttribute("stroke-width", "2");
    chevron.setAttribute("stroke-linecap", "round");
    chevron.setAttribute("stroke-linejoin", "round");
    chevron.setAttribute("aria-hidden", "true");
    var polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", "6 9 12 15 18 9");
    chevron.appendChild(polyline);
    trigger.appendChild(chevron);

    var listbox = document.createElement("ul");
    listbox.className = "ts-select-listbox";
    listbox.id = listboxId;
    listbox.setAttribute("role", "listbox");
    listbox.hidden = true;

    wrapper.appendChild(trigger);
    wrapper.appendChild(listbox);

    syncTriggerLabel(select);
  }

  function highlightedOption(listbox) {
    return listbox.querySelector(".ts-select-option.highlighted");
  }

  function openListbox(select) {
    closeAllListboxes();
    var wrapper = wrapperOf(select);
    var trigger = triggerOf(select);
    var listbox = listboxOf(select);
    if (!wrapper || !trigger || !listbox) return;
    buildOptions(select, listbox, listbox.id);
    listbox.hidden = false;
    wrapper.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    var current = highlightedOption(listbox);
    trigger.setAttribute("aria-activedescendant", current ? current.id : "");
  }

  function closeListbox(select) {
    var wrapper = wrapperOf(select);
    var trigger = triggerOf(select);
    var listbox = listboxOf(select);
    if (!wrapper || !wrapper.classList.contains("open")) return;
    listbox.hidden = true;
    wrapper.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
  }

  function closeAllListboxes() {
    document.querySelectorAll(".ts-select.open select").forEach(closeListbox);
  }

  function selectOption(select, value) {
    if (select.value !== value) {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    syncTriggerLabel(select);
    closeListbox(select);
    var trigger = triggerOf(select);
    // Ne reprend le focus que si rien d'autre ne l'a pris entre-temps (ex.
    // draft-custom-archetype.js focus un champ de poids sur "Custom").
    if (document.activeElement === trigger || document.activeElement === document.body) {
      trigger.focus();
    }
  }

  function moveHighlight(listbox, delta) {
    var options = Array.prototype.slice.call(listbox.querySelectorAll(".ts-select-option"));
    if (!options.length) return;
    var current = highlightedOption(listbox);
    var index = current ? options.indexOf(current) : -1;
    index = (index + delta + options.length) % options.length;
    options.forEach(function (opt) {
      opt.classList.remove("highlighted");
    });
    options[index].classList.add("highlighted");
    options[index].scrollIntoView({ block: "nearest" });
    var trigger = listbox.closest(".ts-select").querySelector(".ts-select-trigger");
    trigger.setAttribute("aria-activedescendant", options[index].id);
  }

  // --- Enrichissement au chargement initial + après chaque swap htmx ---
  document.addEventListener("htmx:load", function (event) {
    var root = event.target;
    if (root.matches && root.matches("select")) enhance(root);
    if (root.querySelectorAll) root.querySelectorAll("select").forEach(enhance);
  });

  // --- Interaction, déléguée (survit aux swaps sans re-binding) ---
  // Un <li> d'option n'est pas focusable : le mousedown dessus fait perdre le
  // focus au trigger AVANT le click (comportement par défaut du navigateur),
  // ce qui déclenche `focusout` -> closeListbox -> liste masquée -> le click
  // qui suit n'atteint plus un élément visible (aucune sélection appliquée,
  // juste une fermeture). preventDefault sur ce mousedown garde le focus sur
  // le trigger, focusout ne se déclenche pas, le click passe normalement.
  document.addEventListener("mousedown", function (event) {
    if (event.target.closest(".ts-select-listbox")) event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest(".ts-select-trigger");
    if (trigger) {
      var select = wrapperOf(trigger).querySelector("select");
      if (wrapperOf(select).classList.contains("open")) {
        closeListbox(select);
      } else {
        openListbox(select);
      }
      return;
    }
    var option = event.target.closest(".ts-select-option");
    if (option) {
      var listbox = option.closest(".ts-select-listbox");
      var sel = listbox.closest(".ts-select").querySelector("select");
      selectOption(sel, option.dataset.value);
      return;
    }
    if (!event.target.closest(".ts-select")) closeAllListboxes();
  });

  document.addEventListener("keydown", function (event) {
    var wrapper = event.target.closest(".ts-select");
    if (!wrapper) return;
    var select = wrapper.querySelector("select");
    var listbox = listboxOf(select);
    var isOpen = wrapper.classList.contains("open");

    if (event.key === "Escape" && isOpen) {
      event.preventDefault();
      closeListbox(select);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!isOpen) {
        openListbox(select);
        return;
      }
      moveHighlight(listbox, event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter" && isOpen) {
      event.preventDefault();
      var current = highlightedOption(listbox);
      if (current) selectOption(select, current.dataset.value);
      return;
    }
    if (event.key === " " && !isOpen) {
      event.preventDefault();
      openListbox(select);
    }
  });

  document.addEventListener("focusout", function (event) {
    var wrapper = event.target.closest(".ts-select");
    if (!wrapper) return;
    if (wrapper.contains(event.relatedTarget)) return;
    var select = wrapper.querySelector("select");
    closeListbox(select);
  });
})();
