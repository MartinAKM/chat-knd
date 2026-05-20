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
  document.getElementById('s-cols').textContent = data.active;
  state.collection = data.active;
  if (col) {
    document.getElementById('s-chunks').textContent = col.count;
    document.getElementById('s-sources').textContent = col.sources.length;
    renderSources(col.sources, col.source_counts);
  }
}

function renderSources(sources, counts) {
  const el = document.getElementById('source-list');
  if (!sources.length) { el.innerHTML = '<div class="empty">Sem dados.</div>'; return; }
  el.innerHTML = sources.map(s => {
    const safe = s.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return `
    <div class="source-item" data-src="${escHtml(s)}" onclick="selectSource('${safe}')">
      <div class="source-name">${escHtml(s)}</div>
      <div class="source-count">${(counts[s]||0)} chunk(s)</div>
      <button class="del-btn" title="Deletar arquivo."
        onclick="deleteSource(event,'${safe}')">&#x2715;</button>
    </div>`;
  }).join('');
  filterSources();
}

function filterSources() {
  const q = (document.getElementById('source-filter-input').value || '').toLowerCase();
  document.querySelectorAll('.source-item').forEach(el => {
    el.style.display = el.dataset.src.toLowerCase().includes(q) ? '' : 'none';
  });
}

async function deleteSource(e, src) {
  e.stopPropagation();
  const result = await Swal.fire({
    text: 'Tem certeza que deseja deletar esse Chunk?',
    icon: 'question',
    showCancelButton: true,
    confirmButtonColor: '#1e3a5f',
    cancelButtonColor: '#6b7280',
    confirmButtonText: 'Deletar',
    cancelButtonText: 'Cancelar',
    heightAuto: false,
    customClass: {
      popup: 'custom-swal',
      confirmButton: 'custom-confirm-btn',
      cancelButton: 'custom-cancel-btn'
    }
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
      Swal.fire({ 
        text: 'Chunk removido com sucesso.', 
        icon: 'success',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
      if (state.source === src) clearSearch();
      await loadStats();
    } else {
      Swal.fire({ 
        text: data.error || 'unknown', 
        icon: 'error',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
    }
  } catch (err) {
    Swal.fire({ 
      text: err.message, 
      icon: 'error',
      confirmButtonColor: '#1e3a5f',
      heightAuto: false,
      customClass: {
        popup: 'custom-swal',
      }
    });
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
  list.innerHTML = '<div class="empty">Carregando…</div>';
  document.getElementById('pagination').style.display = 'none';

  let data;
  if (state.mode === 'semantic') {
    semBtn.disabled = true;
    semBtn.textContent = 'Procurando…';
    try {
      data = await api(`/api/semantic-search?q=${encodeURIComponent(state.query)}`);
    } finally {
      semBtn.disabled = false;
      semBtn.textContent = 'Busca Semântica';
    }
    if (data.error) {
      list.innerHTML = `<div class="empty">${escHtml(data.error)}</div>`;
      document.getElementById('mode-label').textContent = 'Busca semântica falhou';
      return;
    }
    const results = data.results || [];
    renderChunks(results);
    document.getElementById('mode-label').textContent =
      `Busca Semântica: "${state.query}" — ${results.length} chunk(s) acima de 50% de proximidade`;
  } else if (state.mode === 'search') {
    data = await api(`/api/search?q=${encodeURIComponent(state.query)}`);
    renderChunks(data.results || [], state.query);
    document.getElementById('mode-label').textContent =
      `Busca por Texto: "${state.query}" — ${(data.results||[]).length} chunk(s)`;
  } else {
    let url = `/api/documents?page=${state.page}&page_size=${PAGE_SIZE}`;
    if (state.source) url += `&source=${encodeURIComponent(state.source)}`;
    data = await api(url);
    state.total = data.total || 0;
    renderChunks(data.items || []);
    document.getElementById('mode-label').textContent = state.source
      ? `Chunks do Arquivo ${state.source}`
      : `Todos os chunks`;
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
    .map(([k, v]) => `<span class="meta-tag">${escHtml(String(v))}</span>`)
    .join('');
  return `<div class="chunk-info">${tags}</div>`;
}

function renderChunks(items, query = '') {
  const list = document.getElementById('chunk-list');
  if (!items.length) {
    list.innerHTML = '<div class="empty">Não foram encontrados chunks.</div>';
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
    n === 0 ? 'Escolher Arquivo(s)…' : n === 1 ? input.files[0].name : `${n} files selected`;
  setIngestStatus('');
}

function setIngestStatus(msg, cls) {
  const el = document.getElementById('ingest-status');
  el.textContent = msg;
  el.className = 'ingest-status' + (cls ? ' ' + cls : '');
}

async function ingestFile() {
  const input = document.getElementById('file-input');
  if (!input.files.length) { setIngestStatus('Selecione um arquivo primeiro.', 'err'); return; }
  const files = Array.from(input.files);
  const btn = document.getElementById('ingest-btn');
  btn.disabled = true;
  btn.classList.add('loading');
  btn.innerHTML = '<span></span><span></span><span></span>';

  let totalChunks = 0;
  const errors = [];

  for (let i = 0; i < files.length; i++) {
    setIngestStatus(`Processando ${i + 1}/${files.length}: ${files[i].name}…`);
    const form = new FormData();
    form.append('file', files[i]);
    try {
      const r = await fetch('/api/ingest', { method: 'POST', body: form });
      const data = await r.json();
      if (data.ok) {
        totalChunks += data.chunks;
      } else {
        errors.push(`${files[i].name}: ${data.error || 'erro desconhecido'}`);
      }
    } catch (e) {
      errors.push(`${files[i].name}: ${e.message}`);
    }
  }

  input.value = '';
  document.getElementById('file-name').textContent = 'Escolher Arquivo(s)…';
  await loadStats();

  if (errors.length === 0) {
    setIngestStatus(`Concluído: ${files.length} arquivos(s), ${totalChunks} chunks armazenados.`, 'ok');
  } else if (errors.length < files.length) {
    setIngestStatus(`Concluído com erros: ${totalChunks} chunks armazenados. Erros: ${errors.join(' | ')}`, 'err');
  } else {
    setIngestStatus('Falhou: ' + errors.join(' | '), 'err');
  }

  btn.disabled = false;
  btn.classList.remove('loading');
  btn.innerHTML = 'Processar';
}

async function clearAllChunks() {
  const result = await Swal.fire({
    text: 'Deletar TODOS Chunks da Collection?',
    icon: 'question',
    showCancelButton: true,
    confirmButtonColor: '#1e3a5f',
    cancelButtonColor: '#6b7280',
    confirmButtonText: 'Deletar Todos',
    cancelButtonText: 'Cancelar',
    heightAuto: false,
    customClass: {
      popup: 'custom-swal',
      confirmButton: 'custom-confirm-btn',
      cancelButton: 'custom-cancel-btn'
    }
  });

  if (!result.isConfirmed) return;

  try {
    const r = await fetch('/api/clear-all', { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      clearSearch();
      await loadStats();
      Swal.fire({ 
        text: `Concluído — ${data.deleted.toLocaleString()} chunk(s) removido(s).`, 
        icon: 'success',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
    } else {
      Swal.fire({ 
        text: data.error || 'unknown', 
        icon: 'error',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
    }
  } catch (err) {
    Swal.fire({ 
      text: err.message, 
      icon: 'error',
      confirmButtonColor: '#1e3a5f',
      heightAuto: false,
      customClass: {
        popup: 'custom-swal',
      }
    });
  }
}

async function switchCollection(name) {
  const r = await fetch('/api/switch-collection', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ collection: name }),
  });
  const data = await r.json();
  if (!data.ok) {
    Swal.fire({
      text: data.error || 'Erro ao trocar coleção.',
      icon: 'error',
      confirmButtonColor: '#1e3a5f',
      heightAuto: false,
      customClass: { popup: 'custom-swal' },
    });
    return;
  }
  document.querySelector('.modal-backdrop').classList.remove('open');
  await loadStats();
  clearSearch();
}

async function resetCollection(colName) {
  const result = await Swal.fire({
    text: `Tem certeza que deseja deletar a coleção "${colName}"?`,
    icon: 'question',
    showCancelButton: true,
    confirmButtonColor: '#1e3a5f',
    cancelButtonColor: '#6b7280',
    confirmButtonText: 'Deletar',
    cancelButtonText: 'Cancelar',
    heightAuto: false,
    customClass: {
      popup: 'custom-swal',
      confirmButton: 'custom-confirm-btn',
      cancelButton: 'custom-cancel-btn'
    }
  });

  if (!result.isConfirmed) return;

  try {
    const r = await fetch('/api/reset-collection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ collection: colName }),
    });
    const data = await r.json();
    if (data.ok) {
      Swal.fire({
        text: 'Coleção deletada com sucesso.',
        icon: 'success',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
      clearSearch();
      await loadStats();
    } else {
      Swal.fire({
        text: data.error || 'desconhecido', 
        icon: 'error',
        confirmButtonColor: '#1e3a5f',
        heightAuto: false,
        customClass: {
          popup: 'custom-swal',
        }
      });
    }
  } catch (err) {
    Swal.fire({
      text: err.message, 
      icon: 'error',
      confirmButtonColor: '#1e3a5f',
      heightAuto: false,
      customClass: {
        popup: 'custom-swal',
      }
    });
  }
}

// ── Auth ───────────────────────────────────────────────────────────────────

let _currentUserId = null;

async function initUser() {
  try {
    const res = await fetch("/api/auth/me");
    if (res.status === 401) { window.location.href = "/login"; return; }
    const user = await res.json();
    _currentUserId = user.id;
    document.getElementById("header-user-name").textContent = user.name;
    if (user.role === "admin") {
      document.getElementById("users-btn").style.display = "";
    }
  } catch (_) {}
}

// ── Users modal ────────────────────────────────────────────────────────────

function openUsersModal() {
  document.querySelector('.modal-backdrop-users').classList.add('open');
  loadUsers();
}

function closeUsersModal(event) {
  if (event.target.classList.contains('modal-backdrop-users')) {
    document.querySelector('.modal-backdrop-users').classList.remove('open');
  }
}

async function loadUsers() {
  const container = document.getElementById('users-list');
  container.innerHTML = '<div class="empty">Carregando…</div>';
  const data = await api('/api/users');
  if (!data.users || !data.users.length) {
    container.innerHTML = '<div class="empty">Nenhum usuário encontrado.</div>';
    return;
  }
  container.innerHTML = data.users.map(u => {
    const initials = (u.name[0] + u.surname[0]).toUpperCase();
    const isAdmin  = u.role === 'admin';
    const isSelf   = u.id === _currentUserId;
    const newRole  = isAdmin ? 'user' : 'admin';
    const btnLabel = isAdmin ? 'Remover Admin' : 'Tornar Admin';
    const safeId   = u.id.replace(/'/g, "\\'");
    return `
    <div class="user-card">
      <div class="user-avatar">${escHtml(initials)}</div>
      <div class="user-info">
        <div class="user-name">${escHtml(u.name)} ${escHtml(u.surname)}</div>
        <div class="user-email">${escHtml(u.email)}</div>
      </div>
      <div class="user-role-badge ${isAdmin ? 'is-admin' : ''}">${isAdmin ? 'ADMIN' : 'USUÁRIO'}</div>
      <button class="user-role-btn ${isAdmin ? 'demote' : 'promote'}"
        ${isSelf ? 'disabled title="Não é possível alterar o próprio perfil."' : ''}
        onclick="setUserRole('${safeId}','${newRole}')">
        ${escHtml(btnLabel)}
      </button>
    </div>`;
  }).join('');
}

async function setUserRole(userId, role) {
  const r = await fetch('/api/auth/set-role', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, role }),
  });
  const data = await r.json();
  if (data.ok) {
    await loadUsers();
  } else {
    Swal.fire({
      text: data.error || 'Erro ao alterar perfil.',
      icon: 'error',
      confirmButtonColor: '#1e3a5f',
      heightAuto: false,
      customClass: { popup: 'custom-swal' },
    });
  }
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
}

// ── Init ───────────────────────────────────────────────────────────────────

initUser();
loadStats();

function renderCollections(collections, active) {
  const container = document.getElementById('collections-list');
  const ICON_DELETE = `<svg xmlns="http://www.w3.org/2000/svg" width="25" height="25" viewBox="0 0 24 24" fill="currentColor"> <path d="M9 3V4H4V6H5V19C5 20.1 5.9 21 7 21H17C18.1 21 19 20.1 19 19V6H20V4H15V3H9M7 6H17V19H7V6M9 8V17H11V8H9M13 8V17H15V8H13Z"/></svg>`;
  const ICON_VIEW = `<svg xmlns="http://www.w3.org/2000/svg" width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"> <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/> <circle cx="12" cy="12" r="3"/> </svg>`;
  container.innerHTML = collections.map(col => {
    const isActive = col.name === active;
    const safeName = col.name.replace(/'/g, "\\'");
    return `
    <div class="collection-card">
      <div class="collection-header">
        <div class="collection-name">${escHtml(col.name)}</div>
      </div>
      <div class="collection-info">
        <div class="collection-stats">
          <div class="collection-stat">
            <span class="label">Chunks</span>
            <span class="value">${col.count}</span>
          </div>
          <div class="collection-stat">
            <span class="label">Arquivos</span>
            <span class="value">${col.sources.length}</span>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          ${!isActive ? `<button class="collection-switch-btn" onclick="switchCollection('${safeName}')">${ICON_VIEW}</button>` : ''}
          <button class="collection-delete-btn" onclick="resetCollection('${safeName}')" title="Deletar coleção">
            ${ICON_DELETE}
          </button>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function openModalCollection() {
  const modal = document.querySelector('.modal-backdrop');
  modal.classList.toggle('open');
  const data = await api('/api/stats');
  renderCollections(data.collections, data.active);
}

function closeCollectionActions(event) {
  if (event.target.classList.contains('modal-backdrop')) {
    const backdrop = document.querySelector('.modal-backdrop');
    backdrop.classList.remove('open');
  }
}