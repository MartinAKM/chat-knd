const PAGE_SIZE = 30;
let state = { mode: 'all', source: null, query: '', page: 1, total: 0 };

async function api(path) {
  const r = await fetch(path);
  return r.json();
}

async function loadStats() {
  const data = await api('/api/stats');
  if (data.error) { document.getElementById('s-chunks').textContent = 'Error'; return; }
  const col = data.collections.find(c => c.name === data.active) || data.collections[0];
  document.getElementById('s-cols').textContent = data.collections.length;
  if (col) {
    document.getElementById('s-chunks').textContent = col.count.toLocaleString();
    document.getElementById('s-sources').textContent = col.sources.length;
    document.getElementById('col-badge').textContent = 'collection: ' + col.name;
    renderSources(col.sources, col.source_counts);
  }
}

function renderSources(sources, counts) {
  const el = document.getElementById('source-list');
  if (!sources.length) { el.innerHTML = '<div class="empty">No data ingested yet.</div>'; return; }
  el.innerHTML = sources.map(s => {
    const safe = s.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return `
    <div class="source-item" data-src="${escHtml(s)}" onclick="selectSource('${safe}')">
      <div class="source-name">${escHtml(s)}</div>
      <div class="source-count">${(counts[s]||0)} chunk(s)</div>
      <button class="del-btn" title="Delete all chunks for this file"
        onclick="deleteSource(event,'${safe}')">&#x2715;</button>
    </div>`;
  }).join('');
}

async function deleteSource(e, src) {
  e.stopPropagation();

  const result = await Swal.fire({
    title: `Delete all chunks for '${src}'?`,
    text: 'This cannot be undone.',
    icon: 'warning',
    showCancelButton: true,
    // confirmButtonColor: "#3085d6",
    // cancelButtonColor: "#d33",
    confirmButtonText: 'Delete'
  });

  if (!result.isConfirmed) return;

  try {
    const r = await fetch('/api/delete', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: src }),
    });
    const data = await r.json();
    if (data.ok) {
      Swal.fire('Deleted!', 'Source deleted successfully.', 'success');
      if (state.source === src) clearSearch();
      await loadStats();
    } else {
      Swal.fire('Error', data.error || 'unknown', 'error');
    }
  } catch (err) {
    Swal.fire('Error', err.message, 'error');
  }
}

async function selectSource(src) {
  document.querySelectorAll('.source-item').forEach(el => {
    el.classList.toggle('active', el.dataset.src === src);
  });
  state = { mode: 'source', source: src, query: '', page: 1, total: 0 };
  document.getElementById('search-input').value = '';
  await loadChunks();
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) { clearSearch(); return; }
  state = { mode: 'search', source: null, query: q, page: 1, total: 0 };
  document.querySelectorAll('.source-item').forEach(el => el.classList.remove('active'));
  await loadChunks();
}

async function doSemanticSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) { clearSearch(); return; }
  state = { mode: 'semantic', source: null, query: q, page: 1, total: 0 };
  document.querySelectorAll('.source-item').forEach(el => el.classList.remove('active'));
  await loadChunks();
}

async function clearSearch() {
  document.getElementById('search-input').value = '';
  state = { mode: 'all', source: null, query: '', page: 1, total: 0 };
  document.querySelectorAll('.source-item').forEach(el => el.classList.remove('active'));
  await loadChunks();
}

async function loadChunks() {
  const list = document.getElementById('chunk-list');
  const semBtn = document.getElementById('semantic-btn');
  list.innerHTML = '<div class="empty">Loading…</div>';
  document.getElementById('pagination').style.display = 'none';

  let data;
  if (state.mode === 'semantic') {
    semBtn.disabled = true;
    semBtn.textContent = 'Searching…';
    try {
      data = await api(`/api/semantic-search?q=${encodeURIComponent(state.query)}`);
    } finally {
      semBtn.disabled = false;
      semBtn.textContent = 'Semantic';
    }
    if (data.error) {
      list.innerHTML = `<div class="empty">${escHtml(data.error)}</div>`;
      document.getElementById('mode-label').textContent = 'Semantic search failed';
      return;
    }
    const results = data.results || [];
    renderChunks(results);
    document.getElementById('mode-label').textContent =
      `Semantic: "${state.query}" — ${results.length} chunk(s) above 75% proximity`;
  } else if (state.mode === 'search') {
    data = await api(`/api/search?q=${encodeURIComponent(state.query)}`);
    renderChunks(data.results || [], state.query);
    document.getElementById('mode-label').textContent =
      `Text search: "${state.query}" — ${(data.results||[]).length} chunk(s)`;
  } else {
    let url = `/api/documents?page=${state.page}&page_size=${PAGE_SIZE}`;
    if (state.source) url += `&source=${encodeURIComponent(state.source)}`;
    data = await api(url);
    state.total = data.total || 0;
    renderChunks(data.items || []);
    document.getElementById('mode-label').textContent = state.source
      ? `Chunks from "${state.source}" — ${state.total} total`
      : `All chunks — ${state.total} total`;
    renderPagination();
  }
}

function highlight(text, query) {
  if (!query) return escHtml(text);
  const safe = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return escHtml(text).replace(new RegExp(escHtml(safe), 'gi'), m => `<mark>${m}</mark>`);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function proxClass(pct) {
  if (pct >= 85) return 'prox-high';
  if (pct >= 80) return 'prox-mid';
  return 'prox-low';
}

function renderMeta(meta) {
  if (!meta || !Object.keys(meta).length) return '';
  const tags = Object.entries(meta)
    .map(([k, v]) => `<span class="meta-tag"><b>${escHtml(k)}:</b> ${escHtml(String(v))}</span>`)
    .join('');
  return `<div class="chunk-info">${tags}</div>`;
}

function renderChunks(items, query = '') {
  const list = document.getElementById('chunk-list');
  if (!items.length) {
    list.innerHTML = '<div class="empty">No chunks found.</div>';
    return;
  }
  list.innerHTML = items.map(item => `
    <div class="chunk-card">
      <div class="chunk-meta">
        <span class="chunk-id">${escHtml(item.id)}</span>
        <div style="display:flex;align-items:center;gap:6px">
          ${item.proximity !== undefined
            ? `<span class="prox-badge ${proxClass(item.proximity)}">${item.proximity}%</span>`
            : ''}
          <span class="chunk-source">${escHtml(item.source)}</span>
        </div>
      </div>
      <div class="chunk-text">${highlight(item.text, query)}</div>
      ${renderMeta(item.metadata)}
    </div>`).join('');
}

function renderPagination() {
  const pag = document.getElementById('pagination');
  const pages = Math.ceil(state.total / PAGE_SIZE);
  if (pages <= 1) { pag.style.display = 'none'; return; }
  pag.style.display = 'flex';
  let html = `<button class="page-btn" ${state.page===1?'disabled':''} onclick="goPage(${state.page-1})">&#8592;</button>`;
  for (let p = Math.max(1, state.page-2); p <= Math.min(pages, state.page+2); p++) {
    html += `<button class="page-btn ${p===state.page?'active':''}" onclick="goPage(${p})">${p}</button>`;
  }
  html += `<button class="page-btn" ${state.page===pages?'disabled':''} onclick="goPage(${state.page+1})">&#8594;</button>`;
  pag.innerHTML = html;
}

async function goPage(p) {
  state.page = p;
  await loadChunks();
  document.getElementById('chunk-list').scrollTop = 0;
}

document.getElementById('search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') doSearch();
});

function onFilePick(input) {
  const n = input.files.length;
  document.getElementById('file-name').textContent =
    n === 0 ? 'Choose file(s)…' : n === 1 ? input.files[0].name : `${n} files selected`;
  setIngestStatus('');
}

function setIngestStatus(msg, cls) {
  const el = document.getElementById('ingest-status');
  el.textContent = msg;
  el.className = 'ingest-status' + (cls ? ' ' + cls : '');
}

async function ingestFile() {
  const input = document.getElementById('file-input');
  if (!input.files.length) { setIngestStatus('Select a file first.', 'err'); return; }
  const files = Array.from(input.files);
  const btn = document.getElementById('ingest-btn');
  btn.disabled = true;

  let totalChunks = 0;
  const errors = [];

  for (let i = 0; i < files.length; i++) {
    setIngestStatus(`Ingesting ${i + 1}/${files.length}: ${files[i].name}…`);
    const form = new FormData();
    form.append('file', files[i]);
    try {
      const r = await fetch('/api/ingest', { method: 'POST', body: form });
      const data = await r.json();
      if (data.ok) {
        totalChunks += data.chunks;
      } else {
        errors.push(`${files[i].name}: ${data.error || 'unknown error'}`);
      }
    } catch (e) {
      errors.push(`${files[i].name}: ${e.message}`);
    }
  }

  input.value = '';
  document.getElementById('file-name').textContent = 'Choose file(s)…';
  await loadStats();

  if (errors.length === 0) {
    setIngestStatus(`Done: ${files.length} file(s), ${totalChunks} chunks stored.`, 'ok');
  } else if (errors.length < files.length) {
    setIngestStatus(`Partial: ${totalChunks} chunks stored. Errors: ${errors.join(' | ')}`, 'err');
  } else {
    setIngestStatus('All failed: ' + errors.join(' | '), 'err');
  }

  btn.disabled = false;
}

async function resetCollection() {
  const result = await Swal.fire({
    title: 'Drop and recreate the collection?',
    text: 'All indexed chunks will be deleted — you will need to re-ingest all documents.',
    icon: 'warning',
    showCancelButton: true,
    // confirmButtonColor: "#3085d6",
    // cancelButtonColor: "#d33",
    confirmButtonText: 'Reset collection'
  });

  if (!result.isConfirmed) return;

  try {
    const r = await fetch('/api/reset-collection', { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      Swal.fire('Success!', 'Collection reset successfully', 'success');
      clearSearch();
      await loadStats();
    } else {
      Swal.fire('Reset failed', data.error || 'unknown', 'error');
    }
  } catch (err) {
    Swal.fire('Reset failed', err.message, 'error');
  }
}

loadStats();
