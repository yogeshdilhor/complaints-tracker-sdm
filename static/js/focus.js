/* Focus view: click a document in the index to show it in the viewer. */
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.doc-nav-item');
    if (!btn) { return; }
    var frame = document.getElementById('doc-frame');
    if (!frame) { return; }
    document.querySelectorAll('.doc-nav-item').forEach(function (b) { b.classList.remove('active'); });
    btn.classList.add('active');
    frame.src = btn.dataset.src;
  });
})();
