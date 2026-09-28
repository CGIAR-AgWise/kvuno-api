/* kvuno Data Quality Dashboard — stats, conflicts, duplicates, sources */
(function () {
  'use strict';

  var state = { page: 1, perPage: 50 };

  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    if (s === null || s === undefined) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  // ── Stats ───────────────────────────────────────────────────

  function loadStats() {
    fetch('/api/v1/quality/stats')
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.error) throw new Error(s.error);
        $('stat-records').textContent = (s.total_records || 0).toLocaleString();
        $('stat-conflicts').textContent = (s.total_conflicts || 0).toLocaleString();
        $('stat-files').textContent = (s.total_files || 0).toLocaleString();
        $('stat-coverage').textContent = (s.coverage_pct || 0) + '%';
        renderSourceBars(s.conflicts_by_source || []);
      })
      .catch(function (err) {
        showToast('Failed to load stats: ' + err.message, 'danger');
      });
  }

  // ── Conflicts ────────────────────────────────────────────────

  var conflictRows = $('conflict-rows');
  var conflictCount = $('conflict-count');
  var conflictPageInfo = $('conflict-page-info');
  var prevBtn = $('page-prev');
  var nextBtn = $('page-next');

  function loadConflicts() {
    var p = new URLSearchParams();
    p.set('page', state.page);
    p.set('per_page', state.perPage);
    var c = $('f-country').value;
    var src = $('f-source').value;
    var q = $('f-search').value;
    if (c) p.set('country', c);
    if (src) p.set('source', src);
    if (q) p.set('search', q);

    fetch('/api/v1/quality/conflicts?' + p.toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) throw new Error(data.error);
        renderConflicts(data);
        updatePagination(data);
        conflictCount.textContent = data.total + ' conflict' + (data.total !== 1 ? 's' : '');
      })
      .catch(function (err) {
        conflictRows.innerHTML = '<tr><td colspan="8" class="text-center text-danger small py-3">' + esc(err.message) + '</td></tr>';
      });
  }

  function renderConflicts(data) {
    var items = data.data || [];
    if (!items.length) {
      conflictRows.innerHTML = '<tr><td colspan="8" class="text-center text-muted small py-3">No conflicts found.</td></tr>';
      return;
    }
    conflictRows.innerHTML = items.map(function (r) {
      return '<tr>'
        + '<td>' + r.id + '</td>'
        + '<td>' + esc(r.country) + '</td>'
        + '<td>' + esc(r.province) + '</td>'
        + '<td>' + esc(r.variety) + '</td>'
        + '<td>' + esc(r.season_type) + '</td>'
        + '<td>' + esc(r.opt_date) + '</td>'
        + '<td>' + esc(r.source) + '</td>'
        + '<td class="text-muted">' + (r.created_at ? r.created_at.slice(0, 10) : '') + '</td>'
        + '</tr>';
    }).join('');
  }

  function updatePagination(data) {
    conflictPageInfo.textContent = 'Page ' + data.current_page + ' of ' + (data.pages || 1);
    prevBtn.disabled = data.current_page <= 1;
    nextBtn.disabled = data.current_page >= (data.pages || 1);
  }

  function goToPage(page) {
    state.page = page;
    loadConflicts();
  }

  // ── Duplicates ──────────────────────────────────────────────

  function loadDuplicates() {
    fetch('/api/v1/quality/conflicts?per_page=10000')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var items = data.data || [];
        var grouped = {};
        items.forEach(function (r) {
          var key = (r.country || '') + '|' + (r.province || '') + '|' + (r.variety || '') + '|' + (r.season_type || '') + '|' + (r.opt_date || '');
          if (!grouped[key]) grouped[key] = { country: r.country, province: r.province, variety: r.variety, season_type: r.season_type, opt_date: r.opt_date, count: 0, sources: {} };
          grouped[key].count++;
          var src = r.source || 'unknown';
          grouped[key].sources[src] = (grouped[key].sources[src] || 0) + 1;
        });
        renderDuplicates(grouped);
      })
      .catch(function (err) {
        $('dup-rows').innerHTML = '<tr><td colspan="7" class="text-center text-danger small py-3">' + esc(err.message) + '</td></tr>';
      });
  }

  function renderDuplicates(grouped) {
    var rows = $('dup-rows');
    var keys = Object.keys(grouped);
    if (!keys.length) {
      rows.innerHTML = '<tr><td colspan="7" class="text-center text-muted small py-3">No duplicates found.</td></tr>';
      return;
    }
    rows.innerHTML = keys.map(function (key) {
      var g = grouped[key];
      var sources = Object.keys(g.sources).map(function (s) { return s + ' (' + g.sources[s] + ')'; }).join(', ');
      return '<tr>'
        + '<td>' + esc(g.country) + '</td>'
        + '<td>' + esc(g.province) + '</td>'
        + '<td>' + esc(g.variety) + '</td>'
        + '<td>' + esc(g.season_type) + '</td>'
        + '<td>' + esc(g.opt_date) + '</td>'
        + '<td><span class="badge bg-warning text-dark">' + g.count + 'x</span></td>'
        + '<td class="small text-muted">' + esc(sources) + '</td>'
        + '</tr>';
    }).join('');
  }

  // ── Source bars ──────────────────────────────────────────────

  function renderSourceBars(sources) {
    var container = $('source-bars');
    if (!sources.length) {
      container.innerHTML = '<p class="text-muted small py-3 text-center">No conflict source data.</p>';
      return;
    }
    var max = sources.reduce(function (m, s) { return Math.max(m, s.count); }, 1);
    container.innerHTML = sources.map(function (s) {
      var pct = Math.round(s.count / max * 100);
      return '<div class="bar-cell"><span style="width:120px;" class="text-muted small text-end">' + esc(s.source) + '</span>'
        + '<div class="bar bg-warning" style="width:' + pct + '%;background:#ffc107;"></div>'
        + '<span class="small fw-semibold">' + s.count + '</span></div>';
    }).join('');
  }

  // ── Events ───────────────────────────────────────────────────

  var debounce;
  function onFilter() {
    clearTimeout(debounce);
    debounce = setTimeout(function () { state.page = 1; loadConflicts(); }, 300);
  }

  $('f-country').addEventListener('input', onFilter);
  $('f-source').addEventListener('input', onFilter);
  $('f-search').addEventListener('input', onFilter);

  $('clear-conflict-filters').addEventListener('click', function () {
    $('f-country').value = '';
    $('f-source').value = '';
    $('f-search').value = '';
    state.page = 1;
    loadConflicts();
  });

  prevBtn.addEventListener('click', function () { if (!prevBtn.disabled) goToPage(state.page - 1); });
  nextBtn.addEventListener('click', function () { if (!nextBtn.disabled) goToPage(state.page + 1); });

  // Tab switching — load duplicates when tab shown
  document.querySelectorAll('#quality-tabs button').forEach(function (btn) {
    btn.addEventListener('shown.bs.tab', function (e) {
      if (e.target.getAttribute('data-bs-target') === '#tab-duplicates') loadDuplicates();
    });
  });

  // ── Init ────────────────────────────────────────────────────

  loadStats();
  loadConflicts();

})();
