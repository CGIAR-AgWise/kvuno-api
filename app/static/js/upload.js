let DB_COLUMNS = [{ value: '', label: '— skip —' }];
let COLUMN_ALIASES = {};
fetch('/ui/columns').then(r => r.json()).then(data => {
  (data.columns || []).forEach(c => DB_COLUMNS.push({ value: c, label: c }));
  COLUMN_ALIASES = data.aliases || {};
});

let completedFiles = [];
let processedCount = 0;

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadMsg = document.getElementById('upload-msg');
const fileList = document.getElementById('file-list');
const mappingSection = document.getElementById('mapping-section');
const mappingCards = document.getElementById('mapping-cards');
const processAllBtn = document.getElementById('process-all-btn');
const processSingleBtn = document.getElementById('process-single-btn');
const processMsg = document.getElementById('process-msg');

const msgClasses = {
  error: 'alert alert-danger py-1 px-2 mb-0',
  success: 'alert alert-success py-1 px-2 mb-0',
};

function showMsg(el, text, type) {
  el.textContent = text;
  el.className = (msgClasses[type] || '') + ' mt-2';
  el.classList.remove('d-none');
}
function hideMsg(el) { el.classList.add('d-none'); }

const maxFileSize = parseInt(document.body.dataset.maxFileSize) || 20 * 1024 * 1024;
const maxFileSizeMB = maxFileSize / (1024 * 1024);

const r = new Resumable({
  target: '/ui/upload/resumable',
  query: {},
  fileType: ['rds', 'parquet'],
  maxFileSize: maxFileSize,
  chunkSize: 2 * 1024 * 1024,
  simultaneousUploads: 3,
  testChunks: true,
  throttleProgressCallbacks: 1,
  maxFilesErrorCallback() {},
  maxFileSizeErrorCallback(file) {
    showMsg(uploadMsg, file.fileName + ' is too large — max ' + maxFileSizeMB + ' MB.', 'error');
  },
  fileTypeErrorCallback(file) {
    showMsg(uploadMsg, 'Only .rds and .parquet files are supported.', 'error');
  },
});

r.assignDrop(dropZone);
r.assignBrowse(fileInput);

let uploadIdx = 0;

r.on('fileAdded', function (file) {
  hideMsg(uploadMsg);
  const ext = file.fileName.split('.').pop().toLowerCase();
  if (!['rds', 'parquet'].includes(ext)) {
    showMsg(uploadMsg, 'Only .rds and .parquet files are supported.', 'error');
    return false;
  }
  dropZone.classList.add('has-file');
  file.uploadIdx = ++uploadIdx;
  renderFileList();
  r.upload();
});

r.on('progress', function () {
  renderFileList();
});

r.on('fileSuccess', function (file) {
  fetch('/ui/upload/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      identifier: file.uniqueIdentifier,
      totalChunks: file.chunks.length,
      filename: file.fileName,
    }),
  })
    .then(res => res.json())
    .then(data => {
      if (data.error) {
        setFileStatus(file.uploadIdx, 'error', data.error);
        return;
      }
      completedFiles.push({
        idx: file.uploadIdx,
        name: file.fileName,
        file: data.file,
        columns: data.columns,
        rows: data.rows || [],
        columnMap: {},
      });
      setFileStatus(file.uploadIdx, 'mapping');
      renderMappingCards();
      mappingSection.classList.remove('d-none');
    })
    .catch(err => setFileStatus(file.uploadIdx, 'error', err.message));
});

r.on('fileError', function (file, message) {
  setFileStatus(file.uploadIdx, 'error', message);
});

dropZone.addEventListener('click', () => fileInput.click());

// ── File list ────────────────────────────────────────────────

function renderFileList() {
  const files = r.files;
  if (!files.length) {
    fileList.classList.add('d-none');
    return;
  }
  fileList.classList.remove('d-none');
  let html = '<div class="vstack gap-2">';
  files.sort((a, b) => a.uploadIdx - b.uploadIdx).forEach(f => {
    const pct = f.progress() > 0 ? Math.round(f.progress() * 100) : 0;
    const status = getFileStatus(f.uploadIdx);
    const row = status === 'mapping' ? '<span class="badge bg-success">Mapped</span>'
      : status === 'error' ? '<span class="badge bg-danger">Error</span>'
      : status === 'completed' ? '<span class="badge bg-success">Done</span>'
      : `<div class="progress" style="height:4px;width:120px;"><div class="progress-bar" style="width:${pct}%"></div></div>`;
    html += `<div class="d-flex align-items-center gap-3 small">
      <span class="fw-medium text-truncate" style="max-width:300px;">${escHtml(f.fileName)}</span>
      ${row}
      <span class="text-muted">${pct}%</span>
    </div>`;
  });
  html += '</div>';
  fileList.innerHTML = html;
}

let fileStatuses = {};

function getFileStatus(idx) {
  return fileStatuses[idx] || 'uploading';
}

function setFileStatus(idx, status, msg) {
  fileStatuses[idx] = status;
  if (msg) console.log(`file ${idx}: ${msg}`);
  renderFileList();
}

// ── Column mapping ───────────────────────────────────────────

function renderMappingCards() {
  if (!completedFiles.length) {
    mappingSection.classList.add('d-none');
    return;
  }
  let html = '';
  completedFiles.forEach((cf, ci) => {
    html += `<div class="card p-3" data-ci="${ci}">
      <div class="d-flex align-items-center justify-content-between mb-2">
        <h3 class="h6 mb-0">${escHtml(cf.name)}</h3>
        <span class="badge bg-secondary">${cf.columns.length} columns</span>
      </div>
      <table class="table table-sm table-borderless mb-0 align-middle">
        <thead><tr class="text-muted small">
          <th>File column</th>
          <th>Sample value</th>
          <th>DB column</th>
        </tr></thead>
        <tbody>`;
    cf.columns.forEach(col => {
      const sample = cf.rows.length ? escHtml(String(cf.rows[0][col] ?? '')) : '';
      const match = DB_COLUMNS.find(c => c.value && c.value.toLowerCase() === col.toLowerCase())
        || (COLUMN_ALIASES[col.toLowerCase()] && DB_COLUMNS.find(c => c.value === COLUMN_ALIASES[col.toLowerCase()]));
      html += `<tr>
        <td><span class="fw-semibold small">${escHtml(col)}</span></td>
        <td><code class="small text-muted">${sample}</code></td>
        <td><select data-ci="${ci}" data-col="${escHtml(col)}" class="form-select form-select-sm col-map">
          ${DB_COLUMNS.map(c =>
            `<option value="${c.value}"${match && match.value === c.value ? ' selected' : ''}>${c.label}</option>`
          ).join('')}
        </select></td>
      </tr>`;
    });
    html += `</tbody></table></div>`;
  });
  mappingCards.innerHTML = html;
  processAllBtn.disabled = false;
  document.querySelectorAll('.col-map').forEach(sel => {
    sel.addEventListener('change', updateColumnMaps);
  });
}

function updateColumnMaps() {
  completedFiles.forEach((cf, ci) => {
    const map = {};
    document.querySelectorAll(`select[data-ci="${ci}"]`).forEach(sel => {
      const col = sel.dataset.col;
      const val = sel.value;
      if (val) map[col] = val;
    });
    cf.columnMap = map;
  });
}

// ── Process ──────────────────────────────────────────────────

processAllBtn.addEventListener('click', () => processFiles(completedFiles.map(cf => cf.file)));

async function processFiles(files) {
  hideMsg(processMsg);
  processAllBtn.disabled = true;
  let ok = 0, fail = 0;
  for (const file of files) {
    const cf = completedFiles.find(c => c.file === file);
    if (!cf) continue;
    try {
      const res = await fetch('/ui/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: cf.file, column_map: cf.columnMap }),
      });
      const data = await res.json();
      if (!res.ok) {
        fail++;
        showMsg(processMsg, `${cf.name}: ${data.error || 'Failed'}`, 'error');
      } else {
        ok++;
      }
    } catch (err) {
      fail++;
      showMsg(processMsg, `${cf.name}: ${err.message}`, 'error');
    }
  }
  processAllBtn.disabled = false;
  const total = ok + fail;
  if (fail === 0) {
    showMsg(processMsg, `All ${total} file(s) sent for processing.`, 'success');
    setTimeout(() => { window.location.href = '/ui/jobs'; }, 1200);
  } else {
    showMsg(processMsg, `${ok}/${total} file(s) submitted. ${fail} failed.`, 'error');
  }
}

// ── Helpers ──────────────────────────────────────────────────

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
