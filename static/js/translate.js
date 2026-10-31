// Toggles between a complaint's summary and its cached translation (complaints/views.py
// complaint_translate fetches and caches it the first time; this only ever runs after that,
// swapping two already-rendered paragraphs, so it needs no request of its own.
(function () {
  "use strict";
  document.body.addEventListener("click", function (e) {
    var btn = e.target.closest(".lang-toggle[data-swap]");
    if (!btn) return;
    var wrap = document.getElementById(btn.dataset.swap);
    var orig = wrap && wrap.querySelector(".lang-orig");
    var trans = wrap && wrap.querySelector(".lang-trans");
    if (!orig || !trans) return;
    orig.hidden = !orig.hidden;
    trans.hidden = !trans.hidden;
    btn.textContent = orig.hidden ? btn.dataset.labelOrig : btn.dataset.labelTrans;
  });
})();
