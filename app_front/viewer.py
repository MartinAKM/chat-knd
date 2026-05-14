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
from chroma_store import get_collection, upsert_chunks
from cleaner import clean_text, is_good_chunk, strip_rotina_block
from reader import SUPPORTED_EXTENSIONS, extract_text
from chat_api.chat import generate as chat_generate


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

load_dotenv()

CHROMA_PATH = os.getenv("CHROMA_PATH", "chroma_data")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "documents")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
PORT = int(os.getenv("VIEWER_PORT", "8001"))
STATIC_DIR = Path(__file__).parent

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
}


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
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path == "/":
                self.serve_static("viewer.html")

            elif path in ("/viewer.css", "/viewer.js"):
                self.serve_static(path.lstrip("/"))

            elif path == "/chat":
                self.serve_static("chat.html")

            elif path in ("/chat.css", "/chat.js"):
                self.serve_static(path.lstrip("/"))

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
            elif parsed.path == "/api/chat":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                question = body.get("question", "").strip()
                history  = body.get("history", [])
                if not question:
                    self.send_json({"error": "Missing 'question' field"})
                else:
                    self.send_json(chat_generate(question, history))
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
