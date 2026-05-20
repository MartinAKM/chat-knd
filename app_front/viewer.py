import json, os, socketserver, sys, tempfile, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
import chromadb

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "doc_reader"))
sys.path.insert(0, str(_ROOT))

from chunker import chunk_text
from chroma_store import get_collection, invalidate_collection_cache, upsert_chunks
from cleaner import clean_text, is_good_chunk, strip_rotina_block
from reader import SUPPORTED_EXTENSIONS, extract_with_images
from reranker import rerank, warm_up as reranker_warm_up
from chat_api.chat import stream_generate as chat_stream, _ensure_running as _ensure_ollama, _get_known_clients
from chat_api.history import (
    delete_conversation, get_conversation, list_conversations,
)
from auth.db import (
    authenticate, consume_reset_token, create_reset_token,
    create_session, create_user, delete_session,
    get_session_user, init_db, list_users, send_reset_email,
    set_user_role, SESSION_DAYS,
)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

load_dotenv()
init_db()

CHROMA_PATH     = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL     = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
PORT            = int(os.getenv("VIEWER_PORT", "8001"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL    = os.getenv("SUMMARIZE_MODEL") or os.getenv("CHAT_MODEL", "")
# Documents collection is the fixed ingest target (Processar button always writes here).
DOCS_COLLECTION = os.getenv("CHROMA_COLLECTION", "documents")
# Active collection for viewing — starts as documents, can be switched via /api/switch-collection.
_viewer_state: dict = {"collection": DOCS_COLLECTION}


def _active() -> str:
    return _viewer_state["collection"]
STATIC_DIR = Path(__file__).parent

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
}

# Public auth pages — served without any session check
_AUTH_ROUTES = {
    "/login":          "login.html",
    "/signup":         "signup.html",
    "/reset-password": "reset_password.html",
    "/set-password":   "set_password.html",
}


_chroma_client: chromadb.PersistentClient = None


def get_client() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _chroma_client


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
    return {"collections": result, "active": _active()}


def get_documents(page=1, page_size=30, source=None):
    client = get_client()
    col_name = _active()
    try:
        col = client.get_collection(col_name)
    except Exception:
        return {"error": f"Collection '{col_name}' not found", "items": [], "total": 0}

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
        col = client.get_collection(_active())
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


def do_semantic_search(query: str, min_pct: float = 50.0, n_results: int = 20) -> dict:
    if not query.strip():
        return {"results": [], "query": query}
    try:
        col = get_collection(CHROMA_PATH, EMBED_MODEL, _active())
    except Exception as e:
        return {"results": [], "error": str(e)}

    count = col.count()
    if count == 0:
        return {"results": [], "query": query}

    # Fetch 2× candidates so the reranker has more material to work with.
    fetch_n = min(n_results * 2, count)
    try:
        res = col.query(
            query_texts=[query],
            n_results=fetch_n,
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

    if results:
        order = rerank(query, [r["text"] for r in results])
        results = [results[i] for i in order]

    return {"results": results[:n_results], "query": query}


def handle_delete(body_bytes: bytes):
    try:
        source = json.loads(body_bytes).get("source", "").strip()
    except Exception:
        return {"ok": False, "error": "Invalid JSON body"}
    if not source:
        return {"ok": False, "error": "Missing 'source' field"}
    col_name = _active()
    client = get_client()
    try:
        col = client.get_collection(col_name)
    except Exception:
        return {"ok": False, "error": f"Collection '{col_name}' not found"}
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


def handle_reset_collection(col_name: str | None = None):
    col_name = col_name or _active()
    invalidate_collection_cache(CHROMA_PATH, EMBED_MODEL, col_name)
    client = get_client()
    try:
        client.delete_collection(col_name)
    except Exception:
        pass
    get_collection(CHROMA_PATH, EMBED_MODEL, col_name)
    return {"ok": True}


def handle_clear_all_chunks():
    """Delete every chunk from the active collection without dropping it."""
    client = get_client()
    try:
        col = client.get_collection(_active())
    except Exception:
        return {"ok": True, "deleted": 0}
    total = col.count()
    if total == 0:
        return {"ok": True, "deleted": 0}
    deleted = 0
    offset = 0
    while offset < total:
        batch = col.get(limit=500, offset=offset, include=[])
        ids = batch["ids"]
        if not ids:
            break
        col.delete(ids=ids)
        deleted += len(ids)
        offset += len(ids)
    return {"ok": True, "deleted": deleted}


def do_ingest(rfile, content_type: str, content_length: int):
    filename, file_bytes = _parse_upload(rfile, content_type, content_length)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return {"ok": False, "error": f"Unsupported file type '{suffix}'. Allowed: {', '.join(SUPPORTED_EXTENSIONS)}"}

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        if VISION_MODEL:
            _ensure_ollama()
        raw = extract_with_images(tmp_path, OLLAMA_BASE_URL, VISION_MODEL)
        text = clean_text(strip_rotina_block(raw))
        chunks = [c for c in chunk_text(text) if is_good_chunk(c)]
        if not chunks:
            return {"ok": False, "error": "No usable content found after cleaning."}
        col = get_collection(CHROMA_PATH, EMBED_MODEL, DOCS_COLLECTION)
        upsert_chunks(col, filename, chunks)
        return {"ok": True, "filename": filename, "chunks": len(chunks)}
    finally:
        tmp_path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logs

    # ── Auth helpers ──────────────────────────────────────────────────────

    def _session_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[8:].strip() or None
        return None

    def _current_user(self) -> dict | None:
        token = self._session_token()
        return get_session_user(token) if token else None

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _require_role(self, role: str = "user", api: bool = False) -> dict | None:
        """Return user if authenticated with sufficient role, else send 401/403/redirect."""
        user = self._current_user()
        if not user:
            if api:
                self.send_json({"error": "Unauthorized"}, status=401)
            else:
                self._redirect("/login")
            return None
        if role == "admin" and user["role"] != "admin":
            if api:
                self.send_json({"error": "Forbidden"}, status=403)
            else:
                self._redirect("/chat")
            return None
        return user

    # ── Response helpers ──────────────────────────────────────────────────

    def send_json(self, data, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, filename: str):
        file_path = STATIC_DIR / filename
        body = file_path.read_bytes()
        content_type = _STATIC_TYPES.get(file_path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        try:
            # ── Public: auth pages & shared static assets ──────────────────
            if path in _AUTH_ROUTES:
                self.serve_static(_AUTH_ROUTES[path])
                return

            if path == "/auth.css":
                self.serve_static("auth.css")
                return

            # Static assets (CSS/JS) are public — data is behind API auth
            if path in ("/viewer.css", "/viewer.js", "/chat.css", "/chat.js"):
                self.serve_static(path.lstrip("/"))
                return

            # ── Public: auth API ───────────────────────────────────────────
            if path == "/api/auth/me":
                user = self._require_role(api=True)
                if user:
                    self.send_json({k: user[k] for k in ("id", "name", "surname", "email", "role")})
                return

            # ── Admin-only HTML ────────────────────────────────────────────
            if path == "/":
                if self._require_role("admin"):
                    self.serve_static("viewer.html")
                return

            # ── User-or-admin HTML ─────────────────────────────────────────
            if path == "/chat":
                if self._require_role("user"):
                    self.serve_static("chat.html")
                return

            # ── Admin-only API ─────────────────────────────────────────────
            if path == "/api/stats":
                if not self._require_role("admin", api=True): return
                self.send_json(get_stats())

            elif path == "/api/documents":
                if not self._require_role("admin", api=True): return
                page      = int(params.get("page", [1])[0])
                page_size = int(params.get("page_size", [30])[0])
                source    = params.get("source", [None])[0]
                self.send_json(get_documents(page, page_size, source))

            elif path == "/api/search":
                if not self._require_role("admin", api=True): return
                self.send_json(do_search(params.get("q", [""])[0]))

            elif path == "/api/semantic-search":
                if not self._require_role("admin", api=True): return
                self.send_json(do_semantic_search(params.get("q", [""])[0]))

            elif path == "/api/users":
                if not self._require_role("admin", api=True): return
                self.send_json({"users": list_users()})

            # ── User-or-admin API ──────────────────────────────────────────
            elif path == "/api/history":
                user = self._require_role("user", api=True)
                if not user: return
                self.send_json({"conversations": list_conversations(user["id"])})

            elif path.startswith("/api/history/"):
                user = self._require_role("user", api=True)
                if not user: return
                conv_id = path[len("/api/history/"):]
                conv    = get_conversation(conv_id, user["id"])
                if conv:
                    self.send_json(conv)
                else:
                    self.send_response(404)
                    self.end_headers()

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            self.send_json({"error": str(e)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/delete":
                if not self._require_role("admin", api=True): return
                length = int(self.headers.get("Content-Length", 0))
                self.send_json(handle_delete(self.rfile.read(length)))
            elif parsed.path.startswith("/api/history/"):
                user = self._require_role("user", api=True)
                if not user: return
                conv_id = parsed.path[len("/api/history/"):]
                self.send_json({"ok": delete_conversation(conv_id, user["id"])})
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        p = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0))

            # ── Public auth endpoints ──────────────────────────────────────
            if p == "/api/auth/login":
                body  = json.loads(self.rfile.read(length))
                email = body.get("email", "").strip().lower()
                pwd   = body.get("password", "")
                user  = authenticate(email, pwd)
                if not user:
                    self.send_json({"ok": False, "error": "Email ou senha incorretos."})
                    return
                token    = create_session(user["id"])
                cookie   = f"session={token}; Path=/; HttpOnly; Max-Age={SESSION_DAYS * 86400}; SameSite=Lax"
                redirect = "/" if user["role"] == "admin" else "/chat"
                self.send_json({"ok": True, "redirect": redirect}, extra_headers={"Set-Cookie": cookie})

            elif p == "/api/auth/signup":
                body    = json.loads(self.rfile.read(length))
                name    = body.get("name", "").strip()
                surname = body.get("surname", "").strip()
                email   = body.get("email", "").strip().lower()
                pwd     = body.get("password", "")
                if not all([name, surname, email, pwd]):
                    self.send_json({"ok": False, "error": "Todos os campos são obrigatórios."})
                    return
                if len(pwd) < 3:
                    self.send_json({"ok": False, "error": "A senha deve ter no mínimo 3 caracteres."})
                    return
                user = create_user(name, surname, email, pwd)
                if not user:
                    self.send_json({"ok": False, "error": "Este email já está em uso."})
                    return
                self.send_json({"ok": True})

            elif p == "/api/auth/logout":
                token = self._session_token()
                if token:
                    delete_session(token)
                cookie = "session=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax"
                self.send_json({"ok": True}, extra_headers={"Set-Cookie": cookie})

            elif p == "/api/auth/reset-password":
                body  = json.loads(self.rfile.read(length))
                email = body.get("email", "").strip().lower()
                token = create_reset_token(email)
                if token:
                    try:
                        host = self.headers.get("Host", "")
                        base_url = f"http://{host}" if host else None
                        send_reset_email(email, token, base_url)
                    except Exception:
                        pass  # never reveal email existence or SMTP errors
                self.send_json({"ok": True})  # always ok to prevent email enumeration

            elif p == "/api/auth/set-password":
                body  = json.loads(self.rfile.read(length))
                token = body.get("token", "").strip()
                pwd   = body.get("password", "")
                if len(pwd) < 3:
                    self.send_json({"ok": False, "error": "A senha deve ter no mínimo 3 caracteres."})
                    return
                ok = consume_reset_token(token, pwd)
                if ok:
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "Link inválido ou expirado."})

            elif p == "/api/auth/set-role":
                user = self._require_role("admin", api=True)
                if not user: return
                body    = json.loads(self.rfile.read(length))
                user_id = body.get("user_id", "").strip()
                role    = body.get("role", "").strip()
                if user_id == user["id"]:
                    self.send_json({"ok": False, "error": "Não é possível alterar o próprio perfil."})
                    return
                self.send_json({"ok": set_user_role(user_id, role)})

            # ── Admin-only endpoints ───────────────────────────────────────
            elif p == "/api/reset-collection":
                if not self._require_role("admin", api=True): return
                body = {}
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except Exception:
                        pass
                self.send_json(handle_reset_collection(body.get("collection")))

            elif p == "/api/switch-collection":
                if not self._require_role("admin", api=True): return
                body = json.loads(self.rfile.read(length))
                name = body.get("collection", "").strip()
                if not name:
                    self.send_json({"ok": False, "error": "Missing 'collection' field"})
                    return
                try:
                    get_client().get_collection(name)
                except Exception:
                    self.send_json({"ok": False, "error": f"Coleção '{name}' não encontrada."})
                    return
                _viewer_state["collection"] = name
                self.send_json({"ok": True, "collection": name})

            elif p == "/api/clear-all":
                if not self._require_role("admin", api=True): return
                self.send_json(handle_clear_all_chunks())

            elif p == "/api/ingest":
                if not self._require_role("admin", api=True): return
                content_type = self.headers.get("Content-Type", "")
                self.send_json(do_ingest(self.rfile, content_type, length))

            # ── User-or-admin endpoints ────────────────────────────────────
            elif p == "/api/chat":
                user = self._require_role("user", api=True)
                if not user: return
                body            = json.loads(self.rfile.read(length))
                question        = body.get("question", "").strip()
                history         = body.get("history", [])
                conversation_id = body.get("conversation_id") or None
                images          = body.get("images") or None
                if not question and not images:
                    self.send_json({"error": "Missing 'question' or 'images' field"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    for chunk in chat_stream(question, history, conversation_id, user["id"], images):
                        line = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
                        self.wfile.write(line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})


if __name__ == "__main__":
    print(f"ChatKND  ->  http://localhost:{PORT}")
    print(f"  Collection : {DOCS_COLLECTION}")
    print(f"  Data path  : {CHROMA_PATH}")

    print("  Loading embedding model...", end=" ", flush=True)
    get_collection(CHROMA_PATH, EMBED_MODEL, DOCS_COLLECTION)
    print("done")

    print("  Loading reranker model...  ", end=" ", flush=True)
    reranker_warm_up()
    print("done")

    print("  Building client cache...   ", end=" ", flush=True)
    _get_known_clients()
    print("done")

    print("Press Ctrl+C to stop.\n")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
