import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "doc_reader"))

from chroma_store import get_collection  # noqa: E402

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL      = os.getenv("CHAT_MODEL", "gemma4:31b-cloud")
CHROMA_PATH     = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL     = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "documents")

_CONTEXT_RESULTS     = 5   # max chunks from semantic search
_KEYWORD_RESULTS     = 5   # max extra chunks from keyword search
_MIN_PROXIMITY       = 60.0

# Matches typical ERP program codes: 2-6 uppercase letters + 1-6 digits (e.g. CFAB24, EPRO15)
_ERP_CODE_RE = re.compile(r"\b[A-Z]{2,6}\d{1,6}\b")

# Matches ticket numbers: YYMMDD + 3-digit sequence (e.g. 250922016)
_TICKET_RE = re.compile(r"\b2\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}\b")

_SYSTEM_PROMPT = (
    "Você é o ChatKND, um assistente especialista em ERP Oracle Forms. "
    "Sempre responda em português brasileiro, independentemente do idioma da pergunta. "
    "Quando forem fornecidos trechos de contexto, baseie sua resposta neles. "
    "Responda de forma clara e objetiva. Se não tiver certeza, diga isso."
)


# ── Keyword config ─────────────────────────────────────────────────────────

# Common Brazilian/English company suffixes ignored when matching client names
_COMPANY_SUFFIXES = {
    "ltda", "sa", "s/a", "me", "epp", "eireli", "ss", "inc", "corp", "cia",
    "da", "de", "do", "das", "dos", "e",
}

# Regex to extract the client name line from a ticket chunk
_CLIENT_LINE_RE = re.compile(r"^Cliente:\s*(.+)$", re.MULTILINE)

def _load_keywords() -> list[str]:
    path = _ROOT / "keywords.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        terms = data.get("programs", []) + data.get("terms", [])
        return [t.strip() for t in terms if t.strip()]
    except Exception:
        return []

_CONFIGURED_KEYWORDS: list[str] = _load_keywords()

# Cache of client names extracted from ChromaDB ticket chunks.
# Populated on first query; lives for the duration of the server process.
_clients_cache: list[str] | None = None


def _get_known_clients() -> list[str]:
    """Return all unique client names found in ingested ticket chunks."""
    global _clients_cache
    if _clients_cache is not None:
        return _clients_cache
    try:
        col   = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
        total = col.count()
        clients: set[str] = set()
        offset = 0
        while offset < total:
            data = col.get(include=["documents", "metadatas"], limit=500, offset=offset)
            for doc, meta in zip(data["documents"], data["metadatas"]):
                if meta.get("source", "").startswith("ticket_"):
                    m = _CLIENT_LINE_RE.search(doc)
                    if m:
                        clients.add(m.group(1).strip())
            offset += 500
        _clients_cache = list(clients)
    except Exception:
        _clients_cache = []
    return _clients_cache


def _client_in_question(client_name: str, question: str) -> bool:
    """Return True if any significant word of client_name appears in question."""
    words = [
        w for w in client_name.split()
        if w.lower() not in _COMPANY_SUFFIXES and len(w) > 3
    ]
    return bool(words) and any(
        re.search(r"\b" + re.escape(w) + r"\b", question, re.IGNORECASE)
        for w in words
    )


def _extract_keywords(question: str) -> list[str]:
    """
    Return terms from the query that warrant exact-match keyword search.
    Combines auto-detected ERP codes, ticket numbers, configured keywords,
    and client names extracted from ingested ticket chunks.
    """
    found: list[str] = []

    # Auto-detect ERP codes (e.g. CFAB24, EPRO15)
    for match in _ERP_CODE_RE.finditer(question):
        found.append(match.group())

    # Auto-detect ticket numbers (e.g. 250922016 → YYMMDD + 3-digit sequence)
    for match in _TICKET_RE.finditer(question):
        ticket = match.group()
        if ticket not in found:
            found.append(ticket)

    # Configured terms (case-insensitive word-boundary match)
    for term in _CONFIGURED_KEYWORDS:
        if re.search(r"\b" + re.escape(term) + r"\b", question, re.IGNORECASE):
            if term not in found:
                found.append(term)

    # Client names from ingested ticket chunks — matched by significant words
    for client in _get_known_clients():
        if _client_in_question(client, question) and client not in found:
            found.append(client)

    return found


# ── Ollama lifecycle ───────────────────────────────────────────────────────

def _is_running() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_ollama() -> None:
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(0.5)
        if _is_running():
            return
    raise RuntimeError("Ollama did not start within 15 seconds.")


def _ensure_running() -> None:
    if not _is_running():
        _start_ollama()


# ── RAG retrieval ──────────────────────────────────────────────────────────

def _retrieve_context(question: str) -> tuple[str, list[str]]:
    """
    Hybrid retrieval: semantic search + exact keyword match.

    Semantic search finds thematically related chunks.
    Keyword search guarantees that chunks containing ERP program codes or
    configured domain terms are always included, regardless of similarity score.

    Returns (context_block, unique_source_names).
    """
    try:
        col = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
        count = col.count()
        if count == 0:
            return "", []

        # Results keyed by chunk id to deduplicate across both searches.
        # Value: (document_text, metadata, proximity_pct)
        seen: dict[str, tuple[str, dict, float]] = {}

        # 1. Semantic search
        res = col.query(
            query_texts=[question],
            n_results=min(_CONTEXT_RESULTS, count),
            include=["documents", "metadatas", "distances"],
        )
        for doc_id, doc, meta, dist in zip(
            res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            proximity = max(0.0, (1.0 - dist / 2.0) * 100)
            if proximity >= _MIN_PROXIMITY:
                seen[doc_id] = (doc, meta, proximity)

        # 2. Keyword exact-match search for detected ERP codes / configured terms
        keywords = _extract_keywords(question)
        for term in keywords:
            try:
                kw = col.get(
                    where_document={"$contains": term},
                    include=["documents", "metadatas"],
                    limit=_KEYWORD_RESULTS,
                )
            except Exception:
                continue
            for doc_id, doc, meta in zip(kw["ids"], kw["documents"], kw["metadatas"]):
                if doc_id not in seen:
                    # Treat exact keyword match as high confidence
                    seen[doc_id] = (doc, meta, 100.0)

        if not seen:
            return "", []

        # Sort by proximity descending; keyword hits (100.0) surface first
        ranked = sorted(seen.values(), key=lambda x: x[2], reverse=True)
        top    = ranked[: _CONTEXT_RESULTS + _KEYWORD_RESULTS]

        context = "\n---\n".join(entry[0] for entry in top)
        sources: list[str] = []
        for _, meta, _ in top:
            src = meta.get("source", "unknown")
            if src not in sources:
                sources.append(src)

        return context, sources

    except Exception:
        return "", []


# ── Generation ─────────────────────────────────────────────────────────────

def generate(question: str, history: list[dict]) -> dict:
    """
    Retrieve relevant context from ChromaDB, inject it into the current user
    message, then call Ollama with the full conversation history.

    `history` already contains the current question as its last entry
    (the frontend appends it before sending the request).

    Returns {"answer": str, "sources": [str, ...]}.
    """
    _ensure_running()

    context, sources = _retrieve_context(question)

    if context:
        current_content = (
            "Use os trechos abaixo da base de conhecimento para responder à pergunta. "
            "Se não forem relevantes, responda com seu próprio conhecimento.\n\n"
            f"Contexto:\n{context}\n\n"
            f"Pergunta: {question}"
        )
    else:
        current_content = question

    prior_turns = history[:-1] if history else []

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += prior_turns
    messages.append({"role": "user", "content": current_content})

    payload = json.dumps({
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
            return {"answer": data["message"]["content"], "sources": sources}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Ollama returned {e.code}: {body}")
