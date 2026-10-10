/* Drag the divider between the list and the case to give either side more room. */
(function () {
  var KEY = 'splitLeftPx';
  var MIN_LEFT = 300, MIN_RIGHT = 340;

  function init() {
    var bar = document.getElementById('splitter');
    var split = bar && bar.parentElement;
    if (!bar || !split) { return; }

    function clamp(px) {
      var max = split.clientWidth - MIN_RIGHT - bar.offsetWidth;
      return Math.max(MIN_LEFT, Math.min(px, max));
    }
    function set(px, save) {
      split.style.setProperty('--left-w', clamp(px) + 'px');
      if (save) { try { localStorage.setItem(KEY, String(Math.round(clamp(px)))); } catch (e) { /* private mode */ } }
    }
    try {
      var saved = parseInt(localStorage.getItem(KEY), 10);
      if (saved) { set(saved, false); }
    } catch (e) { /* ignore */ }

    var start = null;
    bar.addEventListener('pointerdown', function (e) {
      start = { x: e.clientX, w: split.firstElementChild.getBoundingClientRect().width };
      try { bar.setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ }
      bar.classList.add('dragging');
      document.body.classList.add('resizing');
    });
    bar.addEventListener('pointermove', function (e) {
      if (start) { set(start.w + (e.clientX - start.x), false); }
    });
    function end(e) {
      if (!start) { return; }
      set(start.w + (e.clientX - start.x), true);
      start = null;
      bar.classList.remove('dragging');
      document.body.classList.remove('resizing');
    }
    bar.addEventListener('pointerup', end);
    bar.addEventListener('pointercancel', end);
    bar.addEventListener('dblclick', function () {
      split.style.removeProperty('--left-w');
      try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ }
    });
    bar.addEventListener('keydown', function (e) {
      var step = { ArrowLeft: -40, ArrowRight: 40 }[e.key];
      if (!step) { return; }
      e.preventDefault();
      set(split.firstElementChild.getBoundingClientRect().width + step, true);
    });
    window.addEventListener('resize', function () {
      var cur = split.firstElementChild.getBoundingClientRect().width;
      if (split.style.getPropertyValue('--left-w')) { set(cur, false); }
    });
  }
  document.addEventListener('DOMContentLoaded', init);
})();
