// Powers two things rendered by complaints/widgets.py:
//  - VillageWidget: a Village / MC area radio pair that filters a <select>'s options.
//  - GroupedAddSelect: a <select> whose options can be filtered by another select's choice
//    (data-cascade-of), and whose last option ("+ Add new...") reveals a text input.
// No framework; plain DOM, run once on load (these fields are only used on full-page forms).
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function applyGroupFilter(select, allowed) {
    var before = select.value;
    var stillVisible = false;
    each(select.options, function (opt) {
      var group = opt.dataset.group;
      var show = !group || allowed.indexOf(group) !== -1;
      opt.hidden = !show;
      if (opt.value === before && show) stillVisible = true;
    });
    if (before && !stillVisible) select.value = "";
  }

  function toggleOwnInput(select) {
    var field = select.closest(".cascade-field");
    var input = field && field.querySelector(".cascade-own");
    if (!input) return;
    var opt = select.options[select.selectedIndex];
    var isOwn = !!(opt && opt.dataset.own === "1");
    input.hidden = !isOwn;
    if (!isOwn) input.value = "";
  }

  function wireCascadeSelect(select) {
    var controllerId = select.dataset.cascadeOf;
    if (controllerId) {
      var controller = document.getElementById(controllerId);
      if (controller) {
        var sync = function () {
          var val = controller.value;
          var allowed = val
            ? [val]
            : Array.prototype.map
                .call(controller.options, function (o) {
                  return o.value;
                })
                .filter(Boolean);
          applyGroupFilter(select, allowed);
        };
        controller.addEventListener("change", sync);
        sync();
      }
    }
    select.addEventListener("change", function () {
      toggleOwnInput(select);
    });
    toggleOwnInput(select);
  }

  function wireLocationField(field) {
    var select = field.querySelector("select[data-cascade]");
    var radios = field.querySelectorAll(".loc-kind input[type=radio]");
    if (!select || !radios.length) return;
    var sync = function () {
      var checked = field.querySelector(".loc-kind input:checked");
      applyGroupFilter(select, [checked ? checked.value : "village"]);
    };
    each(radios, function (r) {
      r.addEventListener("change", sync);
    });
    sync();
  }

  document.addEventListener("DOMContentLoaded", function () {
    each(document.querySelectorAll("select[data-cascade]"), wireCascadeSelect);
    each(document.querySelectorAll(".loc-field"), wireLocationField);
  });
})();
