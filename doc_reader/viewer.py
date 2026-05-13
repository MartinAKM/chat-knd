import json
import os
import socketserver
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
import chromadb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chunker import chunk_text
from chroma_store import get_collection, upsert_chunks
from cleaner import clean_text, is_good_chunk, strip_rotina_block
from reader import SUPPORTED_EXTENSIONS, extract_text


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

load_dotenv()

CHROMA_PATH = os.getenv("CHROMA_PATH", "chroma_data")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "documents")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
PORT = int(os.getenv("VIEWER_PORT", "8001"))

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ChromaDB Viewer</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f1f5f9; color: #1e293b; display: flex; flex-direction: column; height: 100vh; }

    header {
      background: #1e3a5f; color: #fff; padding: 14px 24px;
      display: flex; align-items: center; gap: 16px; flex-shrink: 0;
    }
    header h1 { font-size: 1.15rem; font-weight: 600; letter-spacing: .03em; }
    header span { font-size: .8rem; opacity: .7; }

    .stats-bar {
      background: #fff; border-bottom: 1px solid #e2e8f0;
      padding: 10px 24px; display: flex; gap: 32px; flex-shrink: 0;
    }
    .stat { display: flex; flex-direction: column; }
    .stat-label { font-size: .68rem; text-transform: uppercase; letter-spacing: .07em; color: #64748b; }
    .stat-value { font-size: 1.25rem; font-weight: 700; color: #1e3a5f; }

    .workspace { display: flex; flex: 1; overflow: hidden; }

    aside {
      width: 260px; flex-shrink: 0; background: #fff;
      border-right: 1px solid #e2e8f0; display: flex; flex-direction: column;
    }
    .aside-head {
      padding: 12px 16px; font-size: .75rem; text-transform: uppercase;
      letter-spacing: .07em; color: #64748b; border-bottom: 1px solid #e2e8f0;
      font-weight: 600;
    }
    .source-list { overflow-y: auto; flex: 1; }
    .source-item {
      padding: 9px 16px; padding-right: 36px; cursor: pointer;
      border-bottom: 1px solid #f1f5f9; transition: background .15s;
      position: relative;
    }
    .source-item:hover { background: #f8fafc; }
    .source-item.active { background: #e0eaf6; border-left: 3px solid #1e3a5f; }
    .source-name { font-size: .82rem; word-break: break-all; }
    .source-count { font-size: .7rem; color: #64748b; margin-top: 2px; }
    .del-btn {
      position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
      background: none; border: none; color: #94a3b8; cursor: pointer;
      font-size: .85rem; padding: 3px 6px; border-radius: 4px;
      opacity: 0; transition: opacity .15s, color .15s, background .15s;
      line-height: 1;
    }
    .source-item:hover .del-btn { opacity: 1; }
    .del-btn:hover { color: #ef4444; background: #fee2e2; }

    main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

    .toolbar {
      padding: 12px 20px; background: #fff; border-bottom: 1px solid #e2e8f0;
      display: flex; gap: 10px; align-items: center; flex-shrink: 0;
    }
    .toolbar input {
      flex: 1; padding: 7px 12px; border: 1px solid #cbd5e1; border-radius: 6px;
      font-size: .875rem; outline: none; transition: border-color .15s;
    }
    .toolbar input:focus { border-color: #1e3a5f; }
    .toolbar button {
      padding: 7px 16px; background: #1e3a5f; color: #fff; border: none;
      border-radius: 6px; font-size: .875rem; cursor: pointer; transition: opacity .15s;
    }
    .toolbar button:hover { opacity: .85; }
    .toolbar button.secondary {
      background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1;
    }
    .toolbar button.semantic { background: #6d28d9; }
    .toolbar button.semantic:disabled { opacity: .4; cursor: default; }

    .prox-badge {
      font-size: .7rem; font-weight: 700; padding: 2px 9px; border-radius: 99px;
      flex-shrink: 0;
    }
    .prox-high { background: #dcfce7; color: #15803d; }
    .prox-mid  { background: #dbeafe; color: #1d4ed8; }
    .prox-low  { background: #fef3c7; color: #b45309; }

    .mode-label {
      font-size: .75rem; color: #64748b; padding: 8px 20px; flex-shrink: 0;
      background: #f8fafc; border-bottom: 1px solid #e2e8f0;
    }

    .chunk-list { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }

    .chunk-card {
      background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px;
      transition: box-shadow .15s;
    }
    .chunk-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.07); }
    .chunk-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .chunk-id { font-size: .7rem; color: #94a3b8; font-family: monospace; }
    .chunk-source { font-size: .7rem; background: #e0eaf6; color: #1e3a5f; padding: 2px 8px; border-radius: 99px; }
    .chunk-text { font-size: .85rem; line-height: 1.6; color: #334155; white-space: pre-wrap; word-break: break-word; }
    .chunk-text mark { background: #fef08a; border-radius: 2px; }

    .distance-badge {
      font-size: .68rem; color: #64748b; background: #f1f5f9;
      padding: 2px 7px; border-radius: 99px; margin-left: 8px;
    }

    .chunk-info {
      margin-top: 10px; padding-top: 8px; border-top: 1px solid #f1f5f9;
      display: flex; flex-wrap: wrap; gap: 5px;
    }
    .meta-tag {
      font-size: .68rem; background: #f8fafc; border: 1px solid #e2e8f0;
      border-radius: 4px; padding: 2px 8px; color: #475569;
      font-family: monospace;
    }
    .meta-tag b { color: #1e3a5f; font-weight: 600; }

    .empty { text-align: center; color: #94a3b8; padding: 60px 20px; font-size: .9rem; }

    .pagination {
      flex-shrink: 0; padding: 10px 20px; background: #fff;
      border-top: 1px solid #e2e8f0; display: flex; justify-content: center; gap: 8px;
    }
    .page-btn {
      padding: 5px 12px; border: 1px solid #cbd5e1; border-radius: 5px;
      background: #fff; cursor: pointer; font-size: .8rem; color: #475569;
    }
    .page-btn.active { background: #1e3a5f; color: #fff; border-color: #1e3a5f; }
    .page-btn:disabled { opacity: .4; cursor: default; }

    .upload-section {
      padding: 12px 16px; border-bottom: 2px solid #e2e8f0; background: #f8fafc; flex-shrink: 0;
    }
    .upload-title {
      font-size: .7rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: .07em; color: #64748b; margin-bottom: 8px; display: block;
    }
    .upload-row { display: flex; gap: 6px; align-items: stretch; }
    .file-pick-label {
      flex: 1; display: flex; align-items: center;
      background: #fff; border: 1px dashed #94a3b8; border-radius: 6px;
      padding: 6px 10px; font-size: .75rem; color: #64748b;
      cursor: pointer; overflow: hidden; transition: border-color .15s;
      min-width: 0;
    }
    .file-pick-label:hover { border-color: #1e3a5f; color: #1e3a5f; }
    .file-pick-label input[type="file"] { display: none; }
    .file-pick-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ingest-btn {
      padding: 6px 12px; background: #1e3a5f; color: #fff; border: none;
      border-radius: 6px; font-size: .8rem; cursor: pointer; white-space: nowrap;
      transition: opacity .15s;
    }
    .ingest-btn:hover { opacity: .85; }
    .ingest-btn:disabled { opacity: .4; cursor: default; }
    .ingest-status {
      margin-top: 7px; font-size: .73rem; color: #64748b; min-height: 14px;
      word-break: break-word;
    }
    .ingest-status.ok  { color: #16a34a; }
    .ingest-status.err { color: #dc2626; }

    .reset-btn {
      margin-left: auto; padding: 5px 14px; background: none;
      border: 1px solid #fca5a5; border-radius: 6px; color: #ef4444;
      font-size: .78rem; cursor: pointer; transition: background .15s, color .15s;
    }
    .reset-btn:hover { background: #fee2e2; }
  </style>
</head>
<body>

<header>
  <h1>ChromaDB Viewer</h1>
  <span id="col-badge">collection: —</span>
</header>

<div class="stats-bar">
  <div class="stat"><span class="stat-label">Total Chunks</span><span class="stat-value" id="s-chunks">—</span></div>
  <div class="stat"><span class="stat-label">Source Files</span><span class="stat-value" id="s-sources">—</span></div>
  <div class="stat"><span class="stat-label">Collections</span><span class="stat-value" id="s-cols">—</span></div>
  <button class="reset-btn" onclick="resetCollection()" title="Drop the collection and recreate it empty (required after changing embedding model)">Reset Collection</button>
</div>

<div class="workspace">
  <aside>
    <div class="upload-section">
      <span class="upload-title">Ingest file</span>
      <div class="upload-row">
        <label class="file-pick-label">
          <input type="file" id="file-input" accept=".pdf,.docx,.txt,.md" multiple onchange="onFilePick(this)" />
          <span class="file-pick-name" id="file-name">Choose file(s)…</span>
        </label>
        <button class="ingest-btn" id="ingest-btn" onclick="ingestFile()">Ingest</button>
      </div>
      <div class="ingest-status" id="ingest-status"></div>
    </div>
    <div class="aside-head">Sources</div>
    <div class="source-list" id="source-list">
      <div class="empty">Loading…</div>
    </div>
  </aside>

  <main>
    <div class="toolbar">
      <input type="text" id="search-input" placeholder="Search or enter a phrase…" />
      <button onclick="doSearch()">Text</button>
      <button class="semantic" id="semantic-btn" onclick="doSemanticSearch()">Semantic</button>
      <button class="secondary" onclick="clearSearch()">All</button>
    </div>
    <div class="mode-label" id="mode-label">Showing all chunks</div>
    <div class="chunk-list" id="chunk-list">
      <div class="empty">Select a source or search to explore chunks.</div>
    </div>
    <div class="pagination" id="pagination" style="display:none"></div>
  </main>
</div>

<script>
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
    if (!confirm(`Delete all chunks for "${src}"?\nThis cannot be undone.`)) return;
    try {
      const r = await fetch('/api/delete', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: src }),
      });
      const data = await r.json();
      if (data.ok) {
        if (state.source === src) clearSearch();
        await loadStats();
      } else {
        alert('Error: ' + (data.error || 'unknown'));
      }
    } catch (err) {
      alert('Error: ' + err.message);
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
    if (!confirm('Drop and recreate the collection?\nAll indexed chunks will be deleted — you will need to re-ingest all documents.')) return;
    try {
      const r = await fetch('/api/reset-collection', { method: 'POST' });
      const data = await r.json();
      if (data.ok) {
        clearSearch();
        await loadStats();
      } else {
        alert('Reset failed: ' + (data.error || 'unknown'));
      }
    } catch (err) {
      alert('Reset failed: ' + err.message);
    }
  }

  loadStats();
</script>
</body>
</html>
"""


def get_client():
    return chromadb.PersistentClient(path=CHROMA_PATH)


def _collect_source_counts(col, total, batch=500):
    """Page through all metadatas in batches to avoid one huge blocking call."""
    source_counts = {}
    offset = 0
    while offset < total:
        data = col.get(include=["metadatas"], limit=batch, offset=offset)
        for meta in data["metadatas"]:
            src = meta.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        offset += batch
    return source_counts


def get_stats():
    client = get_client()
    collections = client.list_collections()
    result = []
    for col_info in collections:
        col = client.get_collection(col_info.name)
        count = col.count()
        source_counts = _collect_source_counts(col, count) if count > 0 else {}
        result.append({
            "name": col_info.name,
            "count": count,
            "sources": sorted(source_counts.keys()),
            "source_counts": source_counts,
        })
    return {"collections": result, "active": COLLECTION_NAME}


def get_documents(page=1, page_size=30, source=None):
    client = get_client()
    try:
        col = client.get_collection(COLLECTION_NAME)
    except Exception:
        return {"error": f"Collection '{COLLECTION_NAME}' not found", "items": [], "total": 0}

    total = col.count()
    if source:
        data = col.get(where={"source": source}, include=["documents", "metadatas"], limit=total)
    else:
        offset = (page - 1) * page_size
        data = col.get(limit=page_size, offset=offset, include=["documents", "metadatas"])

    items = [
        {"id": doc_id, "text": doc, "source": meta.get("source", "unknown"), "metadata": meta}
        for doc_id, doc, meta in zip(data["ids"], data["documents"], data["metadatas"])
    ]
    return {"items": items, "total": total}


def do_search(query):
    if not query:
        return {"results": []}
    client = get_client()
    try:
        col = client.get_collection(COLLECTION_NAME)
    except Exception:
        return {"results": [], "error": "Collection not found"}

    count = col.count()
    if count == 0:
        return {"results": []}

    data = col.get(
        where_document={"$contains": query},
        include=["documents", "metadatas"],
        limit=min(100, count),
    )
    results = [
        {"id": doc_id, "text": doc, "source": meta.get("source", "unknown"), "metadata": meta}
        for doc_id, doc, meta in zip(data["ids"], data["documents"], data["metadatas"])
    ]
    return {"results": results}


_PT_STOPWORDS = {
    "nao", "nha", "para", "com", "por", "mas", "uma", "umas", "uns",
    "que", "isso", "esse", "este", "esta", "aqui", "ser", "tem", "ter",
    "foi", "sao", "como", "mais", "nos", "nas", "dos", "das", "num",
    "numa", "seu", "sua", "seus", "suas", "ele", "ela", "eles", "elas",
    "tambem", "nao", "pelo", "pela", "pelos", "pelas",
}


def _keyword_score(chunk_text: str, query: str) -> float:
    """
    Fraction of significant query words present in the chunk.
    Uses a 5-char stem prefix to handle basic Portuguese morphology
    (e.g. 'lançamentos' and 'lançamento' both match stem 'lança').
    Returns 1.0 when the query has no significant words so no penalty is applied.
    """
    tokens = re.findall(r"\w+", query.lower())
    significant = [t for t in tokens if len(t) > 3 and t not in _PT_STOPWORDS]
    if not significant:
        return 1.0
    text_lower = chunk_text.lower()
    matched = sum(1 for w in significant if w[:5] in text_lower)
    return matched / len(significant)


def do_semantic_search(query: str, min_pct: float = 75.0, n_results: int = 20) -> dict:
    if not query.strip():
        return {"results": [], "query": query}
    try:
        col = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
    except Exception as e:
        return {"results": [], "error": str(e)}

    count = col.count()
    if count == 0:
        return {"results": [], "query": query}

    try:
        res = col.query(
            query_texts=[query],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        return {"results": [], "error": f"Embedding failed — is Ollama running? ({e})"}

    results = []
    for doc_id, doc, meta, dist in zip(
        res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        # L2 distance → proximity %.
        # nomic-embed-text produces unit-norm vectors, so L2 ∈ [0, 2]:
        #   dist=0  → identical  → 100 %
        #   dist=2  → opposite   →   0 %
        # Using a linear mapping (1 - dist/2) keeps the full range meaningful
        # and distributes scores far more evenly than cosine similarity does
        # (cos_sim clusters all domain-specific docs above 80%, making the
        # threshold useless; linear L2 proximity spreads them across 60-90%).
        pct = round(max(0.0, (1.0 - dist / 2.0) * 100), 1)
        if pct < min_pct:
            continue
        results.append({
            "id": doc_id,
            "text": doc,
            "source": meta.get("source", "unknown"),
            "metadata": meta,
            "proximity": pct,
        })

    results.sort(key=lambda x: x["proximity"], reverse=True)
    print(results)
    return {"results": results, "query": query}


def handle_delete(body_bytes: bytes):
    try:
        source = json.loads(body_bytes).get("source", "").strip()
    except Exception:
        return {"ok": False, "error": "Invalid JSON body"}
    if not source:
        return {"ok": False, "error": "Missing 'source' field"}
    client = get_client()
    try:
        col = client.get_collection(COLLECTION_NAME)
    except Exception:
        return {"ok": False, "error": f"Collection '{COLLECTION_NAME}' not found"}
    existing = col.get(where={"source": source}, include=[])
    count = len(existing["ids"])
    if not count:
        return {"ok": False, "error": f"No chunks found for '{source}'"}
    col.delete(where={"source": source})
    return {"ok": True, "source": source, "deleted": count}


def _parse_upload(rfile, content_type: str, content_length: int):
    """Extract (filename, bytes) from a multipart/form-data request body."""
    boundary = None
    for param in content_type.split(";"):
        param = param.strip()
        if param.startswith("boundary="):
            boundary = ("--" + param[len("boundary="):].strip('"')).encode()
            break
    if not boundary:
        raise ValueError("Missing multipart boundary")

    body = rfile.read(content_length)
    for part in body.split(boundary)[1:]:
        if part in (b"", b"--\r\n", b"--"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, _, data = part.partition(b"\r\n\r\n")
        data = data.rstrip(b"\r\n")
        headers = {}
        for line in headers_raw.split(b"\r\n"):
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.strip().lower().decode()] = v.strip().decode()
        disposition = headers.get("content-disposition", "")
        if "filename=" not in disposition:
            continue
        fname = None
        for item in disposition.split(";"):
            item = item.strip()
            if item.lower().startswith("filename="):
                fname = item[9:].strip("\"'")
                break
        if fname:
            return fname, data
    raise ValueError("No file part found in upload")


def handle_reset_collection():
    client = get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
    return {"ok": True}


def do_ingest(rfile, content_type: str, content_length: int):
    filename, file_bytes = _parse_upload(rfile, content_type, content_length)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return {"ok": False, "error": f"Unsupported file type '{suffix}'. Allowed: {', '.join(SUPPORTED_EXTENSIONS)}"}

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        raw = extract_text(tmp_path)
        text = clean_text(strip_rotina_block(raw))
        chunks = [c for c in chunk_text(text) if is_good_chunk(c)]
        if not chunks:
            return {"ok": False, "error": "No usable content found after cleaning."}
        col = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
        upsert_chunks(col, filename, chunks)
        return {"ok": True, "filename": filename, "chunks": len(chunks)}
    finally:
        tmp_path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logs

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path == "/api/stats":
                self.send_json(get_stats())

            elif path == "/api/documents":
                page = int(params.get("page", [1])[0])
                page_size = int(params.get("page_size", [30])[0])
                source = params.get("source", [None])[0]
                self.send_json(get_documents(page, page_size, source))

            elif path == "/api/search":
                query = params.get("q", [""])[0]
                self.send_json(do_search(query))

            elif path == "/api/semantic-search":
                query = params.get("q", [""])[0]
                self.send_json(do_semantic_search(query))

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            self.send_json({"error": str(e)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/delete":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.send_json(handle_delete(body))
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/reset-collection":
                self.send_json(handle_reset_collection())
            elif parsed.path == "/api/ingest":
                content_type = self.headers.get("Content-Type", "")
                content_length = int(self.headers.get("Content-Length", 0))
                self.send_json(do_ingest(self.rfile, content_type, content_length))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})


if __name__ == "__main__":
    print(f"ChromaDB Viewer  ->  http://localhost:{PORT}")
    print(f"  Collection : {COLLECTION_NAME}")
    print(f"  Data path  : {CHROMA_PATH}")
    print("Press Ctrl+C to stop.\n")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
