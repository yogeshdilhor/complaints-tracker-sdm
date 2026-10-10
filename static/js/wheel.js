/* iPhone-alarm style wheels. Markup comes from complaints/widgets.py. */
(function () {
  function setup(field) {
    field.dataset.ready = '1';
    var ul = field.querySelector('.wheel');
    var items = Array.prototype.slice.call(ul.children);
    var hidden = field.querySelector('input[type=hidden]');
    var own = field.querySelector('.wheel-own');
    var cfg = JSON.parse(field.dataset.config || '{}');
    var isHeadline = cfg.kind === 'headline';
    var index = parseInt(field.dataset.start, 10) || 0;
    var timer = null;
    var form = field.closest('form');

    function row() { return items[0].offsetHeight || 40; }
    function isOwn(li) { return li.hasAttribute('data-own'); }

    // The officer chosen on the other wheel in this form, for titles like "Marked to BDPO ...".
    function officer() {
      var other = form && form.querySelector('.wheel-field[data-name=department]');
      if (other) {
        var sel = other.querySelector('li.selected');
        var o = other.querySelector('.wheel-own');
        if (sel && isOwn(sel)) { return (o && o.value.trim()) || '...'; }
        if (sel && sel.dataset.value) { return sel.dataset.label; }
        return cfg.officer || '...';
      }
      return cfg.officer || '...';
    }
    function fill(template) {
      return template.replace('{officer}', officer()).replace('{portal}', cfg.portal || 'portal');
    }
    function refreshLabels() {
      if (!isHeadline) { return; }
      items.forEach(function (li) { if (!isOwn(li)) { li.textContent = fill(li.dataset.label); } });
    }
    function sync() {
      var li = items[index];
      if (isOwn(li)) { hidden.value = isHeadline ? (own.value || '') : ''; }
      else { hidden.value = isHeadline ? li.textContent : li.dataset.value; }
    }
    function select(i, scroll) {
      index = Math.max(0, Math.min(items.length - 1, i));
      items.forEach(function (li, k) { li.classList.toggle('selected', k === index); });
      var ownSelected = isOwn(items[index]);
      if (own) {
        own.hidden = !ownSelected;
        if (ownSelected && scroll !== false) { own.focus(); }
      }
      sync();
      if (scroll !== false) { ul.scrollTo({ top: index * row(), behavior: 'smooth' }); }
      field.dispatchEvent(new CustomEvent('wheelchange', { bubbles: true }));
    }

    ul.addEventListener('scroll', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var i = Math.round(ul.scrollTop / row());
        if (i !== index) { select(i, false); }
      }, 60);
    });
    items.forEach(function (li, k) {
      li.addEventListener('click', function () { if (!field.dataset.dragged) { select(k, true); } });
    });
    ul.addEventListener('keydown', function (e) {
      var step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (step) { e.preventDefault(); select(index + step, true); }
      if (e.key === 'Home') { e.preventDefault(); select(0, true); }
      if (e.key === 'End') { e.preventDefault(); select(items.length - 1, true); }
    });
    if (own) { own.addEventListener('input', sync); }

    // Mouse drag, so a desktop can spin it like a phone (touch and trackpad scroll natively).
    var drag = null;
    ul.addEventListener('pointerdown', function (e) {
      if (e.pointerType !== 'mouse') { return; }
      drag = { y: e.clientY, top: ul.scrollTop, moved: false };
      field.dataset.dragged = '';
      ul.setPointerCapture(e.pointerId);
    });
    ul.addEventListener('pointermove', function (e) {
      if (!drag) { return; }
      var dy = e.clientY - drag.y;
      if (Math.abs(dy) > 4) { drag.moved = true; field.dataset.dragged = '1'; ul.style.scrollSnapType = 'none'; }
      if (drag.moved) { ul.scrollTop = drag.top - dy; }
    });
    function endDrag() {
      if (!drag) { return; }
      var moved = drag.moved;
      drag = null;
      ul.style.scrollSnapType = '';
      if (moved) { select(Math.round(ul.scrollTop / row()), true); }
      setTimeout(function () { delete field.dataset.dragged; }, 0);
    }
    ul.addEventListener('pointerup', endDrag);
    ul.addEventListener('pointercancel', endDrag);

    // When the officer wheel turns, titles that name the officer follow.
    if (form) {
      form.addEventListener('wheelchange', function (e) {
        if (e.target !== field) { refreshLabels(); sync(); }
      });
      form.addEventListener('input', function (e) {
        if (e.target.classList && e.target.classList.contains('wheel-own') && e.target !== own) {
          refreshLabels(); sync();
        }
      });
    }

    if (own) { own.hidden = !isOwn(items[index]); }
    refreshLabels();
    sync();
    requestAnimationFrame(function () { ul.scrollTop = index * row(); });
  }

  function init(root) {
    (root || document).querySelectorAll('.wheel-field:not([data-ready])').forEach(setup);
  }
  document.addEventListener('DOMContentLoaded', function () { init(document); });
  document.body && document.body.addEventListener('htmx:load', function (e) { init(e.target); });
  document.addEventListener('htmx:load', function (e) { init(e.target); });
})();
