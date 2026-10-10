/* Draws the dashboard charts from the numbers the page embeds (no network calls). */
(function () {
  var holder = document.getElementById('chart-data');
  if (!holder || typeof Chart === 'undefined') { return; }
  var data = JSON.parse(holder.textContent);

  var PALETTE = ['#2c5aa8', '#2f8f83', '#d9962b', '#c2574a', '#7a62b8', '#4d94c9', '#8a9a3c', '#b0577f', '#6b7686'];
  var INK = '#44505f', GRID = '#eceff4';
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif';
  Chart.defaults.font.size = 11;
  Chart.defaults.color = INK;
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.maintainAspectRatio = false;

  function empty(id) {
    var canvas = document.getElementById(id);
    var note = document.createElement('p');
    note.className = 'chart-empty';
    note.textContent = 'Nothing to show for this period.';
    canvas.parentNode.replaceChild(note, canvas);
  }
  function has(set) { return set && set.values && set.values.some(function (v) { return v > 0; }); }
  function colors(n) { var out = []; for (var i = 0; i < n; i++) { out.push(PALETTE[i % PALETTE.length]); } return out; }

  function donut(id, set) {
    if (!has(set)) { return empty('c-' + id); }
    new Chart(document.getElementById('c-' + id), {
      type: 'doughnut',
      data: { labels: set.labels, datasets: [{ data: set.values, backgroundColor: colors(set.values.length), borderColor: '#fff', borderWidth: 2 }] },
      options: { cutout: '58%', plugins: { legend: { position: 'right' } } }
    });
  }
  function bars(id, set, opts) {
    opts = opts || {};
    if (!has(set)) { return empty('c-' + id); }
    new Chart(document.getElementById('c-' + id), {
      type: 'bar',
      data: { labels: set.labels, datasets: [{ data: set.values, backgroundColor: opts.colors || PALETTE[0], borderRadius: 4, maxBarThickness: 26 }] },
      options: {
        indexAxis: opts.horizontal ? 'y' : 'x',
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: !!opts.horizontal, color: GRID }, ticks: { precision: 0 } },
          y: { grid: { display: !opts.horizontal, color: GRID }, ticks: { precision: 0 }, beginAtZero: true }
        }
      }
    });
  }

  var w = data.weekly;
  if (has({ values: w.received.concat(w.closed) })) {
    new Chart(document.getElementById('c-weekly'), {
      type: 'bar',
      data: {
        labels: w.labels,
        datasets: [
          { label: 'Received', data: w.received, backgroundColor: '#2c5aa8', borderRadius: 4, maxBarThickness: 22 },
          { label: 'Closed', data: w.closed, backgroundColor: '#9bb7e3', borderRadius: 4, maxBarThickness: 22 }
        ]
      },
      options: {
        plugins: { legend: { position: 'top', align: 'end' } },
        scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: GRID }, ticks: { precision: 0 } } }
      }
    });
  } else { empty('c-weekly'); }

  donut('by_state', data.by_state);
  donut('by_department', data.by_department);
  donut('by_platform', data.by_platform);
  bars('ageing', data.ageing, { colors: ['#2f7d4f', '#d9962b', '#c2410c', '#b42318'] });
  bars('dept_speed', data.dept_speed, { horizontal: true, colors: '#2f8f83' });
  donut('atr', data.atr);
  bars('by_village', data.by_village, { horizontal: true });
  bars('by_category', data.by_category, { horizontal: true, colors: '#7a62b8' });
  bars('by_tag', data.by_tag, { horizontal: true, colors: '#d9962b' });
})();
