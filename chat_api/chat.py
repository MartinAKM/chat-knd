import json
import os
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

_CONTEXT_RESULTS  = 5
_MIN_PROXIMITY    = 60.0   # discard chunks below 60 % similarity

_SYSTEM_PROMPT = (
    "You are ChatKND, an expert in Oracle Forms ERP. "
    "When context excerpts are provided, ground your answer in them. "
    "Answer clearly and concisely. If you are unsure, say so."
)


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
    Semantic search against ChromaDB.
    Returns (context_block, unique_source_names).
    Both are empty when the collection is empty or no chunk meets the threshold.
    """
    try:
        col = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)
        count = col.count()
        if count == 0:
            return "", []

        res = col.query(
            query_texts=[question],
            n_results=min(_CONTEXT_RESULTS, count),
            include=["documents", "metadatas", "distances"],
        )

        chunks, sources = [], []
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            proximity = max(0.0, (1.0 - dist / 2.0) * 100)
            if proximity < _MIN_PROXIMITY:
                continue
            chunks.append(doc)
            src = meta.get("source", "unknown")
            if src not in sources:
                sources.append(src)

        if not chunks:
            return "", []

        context = "\n---\n".join(chunks)
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
            "Use the following excerpts from the knowledge base to answer the question. "
            "If they are not relevant, answer from your own knowledge.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}"
        )
    else:
        current_content = question

    # Re-build message list: system + prior turns + augmented current question.
    # history[-1] is the raw user question already appended by the frontend;
    # we replace it with the context-augmented version.
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
