const source = new EventSource('/ui/jobs/events');
let allJobs = [];
let activeFilter = 'all';
let searchTerm = '';

document.getElementById('status-tabs').addEventListener('click', function (e) {
  var btn = e.target.closest('button');
  if (!btn) return;
  document.querySelectorAll('#status-tabs .btn').forEach(function (b) { b.classList.remove('active'); });
  btn.classList.add('active');
  activeFilter = btn.dataset.filter;
  renderJobs(allJobs);
});

document.getElementById('job-search').addEventListener('input', function () {
  searchTerm = this.value.toLowerCase().trim();
  renderJobs(allJobs);
});

source.addEventListener('message', function (e) {
  const data = JSON.parse(e.data);
  allJobs = data.jobs || [];
  renderJobs(allJobs);
  highlightJob();
});

function filtered(jobs) {
  return jobs.filter(function (j) {
    if (activeFilter !== 'all' && j.status !== activeFilter) return false;
    if (searchTerm) {
      const name = (j.original_name || j.file || '').toLowerCase();
      if (name.indexOf(searchTerm) === -1) return false;
    }
    return true;
  });
}

function countByStatus(jobs) {
  var c = { completed: 0, processing: 0, error: 0, unknown: 0 };
  jobs.forEach(function (j) { c[j.status] = (c[j.status] || 0) + 1; });
  return c;
}

const STATUS = {
  processing: { color: 'primary',  bar: '#0d6efd', label: 'Processing' },
  completed:  { color: 'success',  bar: '#198754', label: 'Completed' },
  error:      { color: 'danger',   bar: '#dc3545', label: 'Failed' },
  unknown:    { color: 'secondary', bar: '#6c757d', label: 'Pending' },
};

function renderJobs(jobs) {
  var body = document.getElementById('jobs-body');
  if (!body) return;

  var visible = filtered(jobs);
  var counts = countByStatus(jobs);

  if (!visible.length) {
    body.innerHTML = '<div class="text-center py-5"><p class="text-muted mb-2">No matching jobs.</p></div>';
    return;
  }

  var summary = '<div class="d-flex gap-3 mb-3 small flex-wrap align-items-center">'
    + '<span><span class="badge bg-success rounded-pill">' + (counts.completed || 0) + '</span> completed</span>'
    + '<span><span class="badge bg-primary rounded-pill">' + (counts.processing || 0) + '</span> processing</span>'
    + '<span><span class="badge bg-danger rounded-pill">' + (counts.error || 0) + '</span> failed</span>'
    + '<span class="text-muted ms-auto">' + visible.length + ' / ' + jobs.length + ' shown</span>'
    + '</div>';

  var rows = visible.map(function (j) {
    var total = j.total || 1;
    var pct = total > 0 ? Math.round((j.current || 0) / total * 100) : 0;
    var badge = STATUS[j.status] || STATUS.unknown;
    var anim = j.status === 'processing' ? ' progress-bar-striped progress-bar-animated' : '';
    var barW = j.status === 'completed' ? '100' : pct;
    var fmtCur = (j.current || 0).toLocaleString();
    var fmtTot = total.toLocaleString();
    var time = j.mtime ? new Date(j.mtime * 1000).toLocaleString() : '—';
    var name = j.original_name || j.file;

    return '<tr class="job-row" data-file="' + escHtml(j.file) + '" style="cursor:pointer;">'
      + '<td><span class="fw-medium small" title="' + escHtml(j.file) + '">' + escHtml(name) + '</span></td>'
      + '<td><span class="badge rounded-pill bg-' + badge.color + '">' + badge.label + '</span></td>'
      + '<td class="text-nowrap small text-muted">' + fmtCur + ' / ' + fmtTot + '</td>'
      + '<td style="min-width:140px;"><div class="progress" style="height:6px;"><div class="progress-bar' + anim + '" role="progressbar" style="width:' + barW + '%;background-color:' + badge.bar + '"></div></div></td>'
      + '<td class="small text-muted">' + escHtml(j.message || '') + '</td>'
      + '<td class="small text-muted text-nowrap">' + time + '</td>'
      + (j.status === 'error' ? '<td><button class="btn btn-outline-danger btn-sm retry-btn" data-file="' + escHtml(j.file) + '">Retry</button></td>' : '<td></td>')
      + '</tr>';
  }).join('');

  body.innerHTML = summary
    + '<div class="table-responsive"><table class="table table-hover align-middle mb-0">'
    + '<thead class="table-light"><tr><th>File</th><th>Status</th><th>Rows</th><th>Progress</th><th>Message</th><th>Updated</th><th></th></tr></thead>'
    + '<tbody>' + rows + '</tbody>'
    + '</table></div>';

  document.querySelectorAll('.retry-btn').forEach(function (btn) {
    btn.addEventListener('click', retryJob);
  });
  document.querySelectorAll('.job-row').forEach(function (row) {
    row.addEventListener('click', function (e) {
      if (e.target.closest('.retry-btn')) return;
      showJobDetail(this.dataset.file);
    });
  });
}

function retryJob(e) {
  var file = e.target.dataset.file;
  if (!file) return;
  e.target.disabled = true;
  e.target.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  fetch('/ui/process', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file: file }),
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (data.error) { showToast('Retry failed: ' + data.error, 'danger'); }
    e.target.disabled = false;
    e.target.textContent = 'Retry';
  }).catch(function () {
    e.target.disabled = false;
    e.target.textContent = 'Retry';
  });
}

function showJobDetail(file) {
  var modal = new bootstrap.Modal(document.getElementById('job-detail-modal'));
  document.getElementById('detail-title').textContent = file;
  var body = document.getElementById('detail-body');
  body.innerHTML = '<div class="text-center text-muted py-4">Loading…</div>';
  document.getElementById('detail-retry-btn').classList.add('d-none');
  modal.show();

  fetch('/ui/progress/' + encodeURIComponent(file))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var time = data.mtime ? new Date((data.mtime || Date.now() / 1000) * 1000).toLocaleString() : '—';
      var html = '<dl class="row mb-0">'
        + '<dt class="col-sm-4">Status</dt><dd class="col-sm-8"><span class="badge rounded-pill bg-'
        + (STATUS[data.status] || STATUS.unknown).color + '">' + (STATUS[data.status] || STATUS.unknown).label + '</span></dd>'
        + '<dt class="col-sm-4">Rows processed</dt><dd class="col-sm-8">' + (data.current || 0).toLocaleString() + ' / ' + (data.total || 0).toLocaleString() + '</dd>'
        + '<dt class="col-sm-4">Progress</dt><dd class="col-sm-8"><div class="progress" style="height:6px;max-width:200px;"><div class="progress-bar" style="width:' + (data.total > 0 ? Math.round((data.current || 0) / data.total * 100) : 0) + '%"></div></div></dd>'
        + '<dt class="col-sm-4">Message</dt><dd class="col-sm-8">' + escHtml(data.message || '—') + '</dd>'
        + '<dt class="col-sm-4">Last updated</dt><dd class="col-sm-8">' + time + '</dd>'
        + '</dl>';
      body.innerHTML = html;
      if (data.status === 'error') {
        var btn = document.getElementById('detail-retry-btn');
        btn.classList.remove('d-none');
        btn.onclick = function () {
          btn.disabled = true;
          fetch('/ui/process', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file: file }),
          }).then(function () { modal.hide(); }).catch(function () { btn.disabled = false; });
        };
      }
    })
    .catch(function () {
      body.innerHTML = '<div class="alert alert-danger mb-0">Failed to load job details.</div>';
    });
}


function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function highlightJob() {
  var file = decodeURIComponent(location.hash.slice(1));
  if (!file) return;
  var rows = document.querySelectorAll('#jobs-body tbody tr');
  for (var i = 0; i < rows.length; i++) {
    var first = rows[i].querySelector('td:first-child span');
    if (first && first.textContent.trim() === file) {
      rows[i].classList.add('table-primary');
      rows[i].scrollIntoView({ behavior: 'smooth', block: 'center' });
      break;
    }
  }
}
