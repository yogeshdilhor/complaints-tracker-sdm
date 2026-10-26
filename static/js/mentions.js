/* Smart typing in the stage forms.
   "$" opens the kinds of stage and the usual titles (filters as you type; if nothing matches,
   your own words become the stage). In the Add stage form the list opens by itself.
   "@" opens the list of offices (filters as you type; a new office can be typed too).
   "#" opens a calendar. The choices are posted in hidden fields:
   mark_department / mark_new_department / expected_by. */
(function () {
  'use strict';
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var panel = null, state = null;

  function offices(form) {
    if (!form._offices) {
      var el = form.querySelector('#smart-departments-data');
      try { form._offices = el ? JSON.parse(el.textContent) : []; } catch (e) { form._offices = []; }
    }
    return form._offices;
  }
  function hidden(form, name) { return form.querySelector('input[name="' + name + '"]'); }

  function close() {
    if (panel) { panel.remove(); panel = null; }
    state = null;
  }
  function place(field) {
    var r = field.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)) + 'px';
    var below = r.bottom + 4;
    if (below + panel.offsetHeight > window.innerHeight - 8) { below = Math.max(8, r.top - panel.offsetHeight - 4); }
    panel.style.top = below + 'px';
  }
  function make(cls) {
    close();
    panel = document.createElement('div');
    panel.className = 'smart-panel ' + cls;
    panel.setAttribute('role', 'listbox');
    document.body.appendChild(panel);
    panel.addEventListener('mousedown', function (e) { e.preventDefault(); });  // keep the cursor in the field
    return panel;
  }

  // ---- "@": offices
  function officeMatches(form, q) {
    q = q.toLowerCase();
    return offices(form).some(function (o) {
      return o.name.toLowerCase().indexOf(q) !== -1 || o.short.toLowerCase().indexOf(q) !== -1;
    });
  }
  function showOffices(field, form, start, query) {
    var q = query.trim().toLowerCase();
    var all = offices(form);
    var hits = all.filter(function (o) {
      return !q || o.name.toLowerCase().indexOf(q) !== -1 || o.short.toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {
      var sa = (a.short.toLowerCase().indexOf(q) === 0 || a.name.toLowerCase().indexOf(q) === 0) ? 0 : 1;
      var sb = (b.short.toLowerCase().indexOf(q) === 0 || b.name.toLowerCase().indexOf(q) === 0) ? 0 : 1;
      return sa - sb;
    }).slice(0, 8);
    var exact = all.some(function (o) { return o.name.toLowerCase() === q || o.short.toLowerCase() === q; });
    var rows = hits.map(function (o) { return { id: o.id, label: o.short, sub: o.name, isNew: false }; });
    if (q && !exact) { rows.push({ id: '', label: query.trim(), sub: 'Add as a new office', isNew: true }); }
    if (!rows.length) { close(); return; }
    var p = make('smart-offices');
    var index = 0;
    rows.forEach(function (row, i) {
      var item = document.createElement('div');
      item.className = 'smart-item' + (i === 0 ? ' active' : '') + (row.isNew ? ' is-new' : '');
      item.setAttribute('role', 'option');
      var b = document.createElement('b'); b.textContent = row.label;
      var s = document.createElement('small'); s.textContent = row.sub;
      item.appendChild(b); item.appendChild(s);
      item.addEventListener('click', function () { chooseOffice(field, form, start, row); });
      p.appendChild(item);
    });
    place(field);
    state = {
      kind: 'office', explicit: true, rows: rows, move: function (d) {
        state.navigated = true;
        var items = p.querySelectorAll('.smart-item');
        items[index].classList.remove('active');
        index = (index + d + rows.length) % rows.length;
        items[index].classList.add('active');
        items[index].scrollIntoView({ block: 'nearest' });
      },
      pick: function () { chooseOffice(field, form, start, rows[index]); }
    };
  }
  function chooseOffice(field, form, start, row) {
    var caret = field.selectionStart;
    var text = field.value;
    field.value = text.slice(0, start) + row.label + ' ' + text.slice(caret);
    var pos = start + row.label.length + 1;
    field.setSelectionRange(pos, pos);
    hidden(form, 'mark_department').value = row.isNew ? '' : row.id;
    hidden(form, 'mark_new_department').value = row.isNew ? row.label : '';
    form._markedText = row.label;
    close();
    field.focus();
    field.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // ---- "by ": calendar
  function showCalendar(field, form, start, month) {
    var today = new Date();
    var view = month || new Date(today.getFullYear(), today.getMonth(), 1);
    var p = make('smart-calendar');
    function draw() {
      p.innerHTML = '';
      var head = document.createElement('div'); head.className = 'cal-head';
      var prev = document.createElement('button'); prev.type = 'button'; prev.textContent = '‹';
      var next = document.createElement('button'); next.type = 'button'; next.textContent = '›';
      var title = document.createElement('b'); title.textContent = MONTHS[view.getMonth()] + ' ' + view.getFullYear();
      prev.addEventListener('click', function () { view = new Date(view.getFullYear(), view.getMonth() - 1, 1); draw(); place(field); });
      next.addEventListener('click', function () { view = new Date(view.getFullYear(), view.getMonth() + 1, 1); draw(); place(field); });
      head.appendChild(prev); head.appendChild(title); head.appendChild(next);
      p.appendChild(head);
      var grid = document.createElement('div'); grid.className = 'cal-grid';
      ['M', 'T', 'W', 'T', 'F', 'S', 'S'].forEach(function (d) {
        var h = document.createElement('span'); h.className = 'cal-dow'; h.textContent = d; grid.appendChild(h);
      });
      var lead = (new Date(view.getFullYear(), view.getMonth(), 1).getDay() + 6) % 7;  // Monday first
      for (var i = 0; i < lead; i++) { grid.appendChild(document.createElement('span')); }
      var days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
      for (var d = 1; d <= days; d++) {
        (function (day) {
          var cell = document.createElement('button'); cell.type = 'button'; cell.textContent = day;
          var isToday = day === today.getDate() && view.getMonth() === today.getMonth() && view.getFullYear() === today.getFullYear();
          if (isToday) { cell.className = 'today'; }
          cell.addEventListener('click', function () { chooseDate(field, form, start, new Date(view.getFullYear(), view.getMonth(), day)); });
          grid.appendChild(cell);
        })(d);
      }
      p.appendChild(grid);
    }
    draw();
    place(field);
    state = { kind: 'date', move: function () {}, pick: function () {} };
  }
  function iso(d) {
    var m = String(d.getMonth() + 1).padStart(2, '0'), day = String(d.getDate()).padStart(2, '0');
    return d.getFullYear() + '-' + m + '-' + day;
  }
  function chooseDate(field, form, start, date) {
    var label = date.getDate() + ' ' + MONTHS[date.getMonth()] + ' ' + date.getFullYear();
    var caret = field.selectionStart;
    var text = field.value;
    field.value = text.slice(0, start) + label + ' ' + text.slice(caret);
    var pos = start + label.length + 1;
    field.setSelectionRange(pos, pos);
    hidden(form, 'expected_by').value = iso(date);
    form._dateText = label;
    close();
    field.focus();
    field.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // ---- "$": kinds of stage and titles
  function stageMenu(form) {
    if (!form._stages) {
      var el = form.querySelector('#stage-menu-data');
      try { form._stages = el ? JSON.parse(el.textContent) : []; } catch (e) { form._stages = []; }
    }
    return form._stages;
  }
  function showStages(field, form, start, query) {
    var q = query.trim().toLowerCase();
    var rows = stageMenu(form).filter(function (r) {
      return !q || r.label.toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {  // those that start with what was typed come first
      return (a.label.toLowerCase().indexOf(q) === 0 ? 0 : 1) - (b.label.toLowerCase().indexOf(q) === 0 ? 0 : 1);
    }).slice(0, 9);
    var own = q ? { kind: 'own', label: query.trim(), hint: 'No stage like this: add it as your own' } : null;
    if (own) { if (rows.length) { rows.push(own); } else { rows = [own]; } }
    if (!rows.length) { close(); return; }
    var p = make('smart-offices smart-stages');
    var index = 0;
    rows.forEach(function (row, i) {
      var item = document.createElement('div');
      item.className = 'smart-item' + (i === 0 ? ' active' : '') + (row.kind === 'own' ? ' is-new' : '');
      item.setAttribute('role', 'option');
      var b = document.createElement('b'); b.textContent = row.label;
      var s = document.createElement('small'); s.textContent = row.hint;
      item.appendChild(b); item.appendChild(s);
      item.addEventListener('click', function () { chooseStage(field, form, start, row); });
      p.appendChild(item);
    });
    place(field);
    state = {
      kind: 'office', explicit: start > 0 || /\$/.test(field.value), move: function (d) {
        state.navigated = true;
        var items = p.querySelectorAll('.smart-item');
        items[index].classList.remove('active');
        index = (index + d + rows.length) % rows.length;
        items[index].classList.add('active');
        items[index].scrollIntoView({ block: 'nearest' });
      },
      pick: function () { chooseStage(field, form, start, rows[index]); }
    };
  }
  function chooseStage(field, form, start, row) {
    var caret = field.selectionStart;
    var text = field.value;
    field.dataset.autostage = '0';
    if (row.kind === 'load') {            // another kind of stage: open its own form
      close();
      if (window.htmx) { window.htmx.ajax('GET', row.url, { target: '#action-area', swap: 'innerHTML' }); }
      return;
    }
    var label = row.kind === 'own' ? row.label : row.label;
    field.value = text.slice(0, start) + label + (row.kind === 'text' ? '' : ' ') + text.slice(caret);
    var pos = start + label.length + (row.kind === 'text' ? 0 : 1);
    field.setSelectionRange(pos, pos);
    close();
    field.focus();
  }
  // A freshly opened Add stage form puts the cursor in the title and shows the kinds of stage.
  function openComposer(root) {
    var field = root.querySelector && root.querySelector('form[data-smart] input[data-smart-title]');
    if (!field || field.dataset.autostage) { return; }
    var form = field.closest('form');
    if (!stageMenu(form).length) { return; }
    field.dataset.autostage = '1';
    setTimeout(function () { field.focus(); showStages(field, form, 0, field.value); }, 60);
  }
  document.addEventListener('DOMContentLoaded', function () { openComposer(document); });
  document.addEventListener('htmx:afterSettle', function (e) { openComposer(e.target || document); });

  // ---- watching what is typed
  function smartField(el) {
    return el && el.closest && el.closest('form[data-smart], form[data-stage-menu]') &&
      (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && el.type === 'text'));
  }
  document.addEventListener('input', function (e) {
    var field = e.target;
    if (!smartField(field)) { return; }
    var form = field.closest('form[data-smart], form[data-stage-menu]');
    var before = field.value.slice(0, field.selectionStart);
    var dollar = before.match(/(^|\s)\$([^$@#\n]{0,40})$/);
    if (dollar && form.hasAttribute('data-stage-menu')) {
      showStages(field, form, before.length - dollar[2].length - 1, dollar[2]);
      return;
    }
    if (field.dataset.autostage === '1' && field.matches('[data-smart-title]') && !/[@#$]/.test(field.value)) {
      showStages(field, form, 0, field.value);  // still choosing: narrow the list as you type
      return;
    }
    var at = form.hasAttribute('data-smart') && before.match(/(^|\s)@(\S[^@#$\n]{0,40})?$/);
    if (at) {
      var typed = at[2] || '';
      var start = before.length - typed.length - 1;
      // A space after a word that is no known office: that word is a new office, and the
      // mention is over. (An existing office with a long name keeps the list open.)
      if (/\s/.test(typed) && !officeMatches(form, typed.trim())) {
        var word = typed.split(/\s+/)[0];
        hidden(form, 'mark_department').value = '';
        hidden(form, 'mark_new_department').value = word;
        form._markedText = word;
        close();
        return;
      }
      showOffices(field, form, start, typed);
      return;
    }
    if (form.hasAttribute('data-smart') && /(^|\s)#$/.test(before) && e.inputType !== 'deleteContentBackward') {
      showCalendar(field, form, before.length - 1, null);  // the # itself is replaced by the date
      return;
    }
    close();
  });
  document.addEventListener('keydown', function (e) {
    if (!state) { return; }
    if (e.key === 'Escape') { document.querySelectorAll('[data-autostage]').forEach(function (f) { f.dataset.autostage = '0'; }); close(); e.preventDefault(); return; }
    if (state.kind !== 'office') { return; }
    if (e.key === 'ArrowDown') { state.move(1); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { state.move(-1); e.preventDefault(); }
    else if (e.key === 'Enter' || e.key === 'Tab') {
      // The list that opens by itself only takes Enter after the arrow keys were used, so
      // writing your own title and pressing Enter just saves it.
      if (state.explicit || state.navigated) { state.pick(); e.preventDefault(); } else { close(); }
    }
  });
  document.addEventListener('mousedown', function (e) {
    if (panel && !panel.contains(e.target) && !smartField(e.target)) { close(); }
  });
  // A choice only counts if its words are still in the text when the form is saved.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[data-smart]')) { return; }
    var text = Array.prototype.map.call(form.querySelectorAll('input[type=text], textarea'), function (f) { return f.value; }).join('\n');
    if (form._markedText && text.indexOf(form._markedText) === -1) {
      hidden(form, 'mark_department').value = ''; hidden(form, 'mark_new_department').value = '';
    }
    if (form._dateText && text.indexOf(form._dateText) === -1) { hidden(form, 'expected_by').value = ''; }
  }, true);
  window.addEventListener('scroll', close, true);
})();
