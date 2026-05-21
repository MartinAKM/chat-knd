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
sys.path.insert(0, str(_ROOT))

from chroma_store import get_collection          # noqa: E402
from reranker import rerank                      # noqa: E402
import bm25_store                                # noqa: E402
from chat_api.history import (                   # noqa: E402
    append_exchange, create_conversation,
)

OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL         = os.getenv("CHAT_MODEL", "gemma4:31b-cloud")
# Lighter/dedicated vision model for image keyword extraction pre-pass.
# Falls back to CHAT_MODEL if not set.
VISION_EXTRACT_MODEL = os.getenv("SUMMARIZE_MODEL") or CHAT_MODEL
CHROMA_PATH        = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL        = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
DOCS_COLLECTION    = os.getenv("CHROMA_COLLECTION", "documents")
TICKETS_COLLECTION = os.getenv("TICKETS_CHROMA_COLLECTION", "tickets")
CHATS_COLLECTION   = os.getenv("CHATS_COLLECTION", "chats")

_DOC_RESULTS           = 3   # max chunks from documents collection
_TICKET_RESULTS        = 5   # max chunks from tickets collection (dense + BM25)
_KEYWORD_RESULTS       = 5   # max extra ticket chunks from keyword exact-match
_DATE_RESULTS          = 10  # max extra ticket chunks from date-filtered search
_CHATS_RESULTS         = 3   # max chunks from learned-chats collection
_MIN_PROXIMITY         = 60.0  # minimum score to include a chunk in LLM context
_MAX_HISTORY_TURNS     = 10  # prior messages kept in LLM context (5 exchanges)

# Matches typical ERP program codes: 2-6 letters + 1-6 digits (e.g. CFAB24, cext24, EPRO15)
_ERP_CODE_RE = re.compile(r"\b[A-Za-z]{2,6}\d{1,6}\b")

# Matches ticket numbers: YYMMDD + 3-digit sequence (e.g. 250922016)
_TICKET_RE = re.compile(r"\b2\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}\b")

# Matches ERP/Oracle message codes (e.g. KND-00423, ORA-06512)
_MSG_CODE_RE = re.compile(r"\b(KND-\d+|ORA-\d+)\b", re.IGNORECASE)

# Matches "pedido de serviço <code>" / "pedido serviço <code>" in the question
_PEDIDO_RE = re.compile(
    r"\bpedido\s+de\s+servi[çc]o\s*[:\-]?\s*([A-Za-z0-9][\w\-]*)",
    re.IGNORECASE,
)

# ── Date extraction ────────────────────────────────────────────────────────

_MONTH_MAP = {
    "janeiro": "01", "fevereiro": "02",
    "março": "03",   "marco": "03",
    "abril": "04",   "maio": "05",    "junho": "06",
    "julho": "07",   "agosto": "08",  "setembro": "09",
    "outubro": "10", "novembro": "11", "dezembro": "12",
}
_YEAR_RE        = re.compile(r"\b20(\d{2})\b")
_MONTH_NAME_RE  = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _MONTH_MAP) + r")\b", re.IGNORECASE
)
_DAY_RE         = re.compile(r"\bdia\s+(0?[1-9]|[12]\d|3[01])\b", re.IGNORECASE)
# DD/MM/YYYY  e.g. 15/03/2026
_SLASH_FULL_RE  = re.compile(r"\b(0?[1-9]|[12]\d|3[01])/(0[1-9]|1[0-2])/(20\d{2})\b")
# MM/YYYY     e.g. 03/2026
_SLASH_MONTH_RE = re.compile(r"\b(0[1-9]|1[0-2])/(20\d{2})\b")


def _extract_date_prefix(question: str) -> str | None:
    """
    Detect a date reference and return the ticket ID prefix for that period.

    "2026"                      → "26"
    "03/2026"                   → "2603"
    "15/03/2026"                → "260315"
    "março de 2026"             → "2603"
    "dia 15 de março de 2026"   → "260315"
    Returns None when no date is found.
    """
    # DD/MM/YYYY — most specific, try first
    m = _SLASH_FULL_RE.search(question)
    if m:
        dd, mm, yyyy = m.group(1).zfill(2), m.group(2), m.group(3)[2:]
        return yyyy + mm + dd

    # MM/YYYY
    m = _SLASH_MONTH_RE.search(question)
    if m:
        mm, yyyy = m.group(1), m.group(2)[2:]
        return yyyy + mm

    # Year (required for all word-based formats below)
    year_m = _YEAR_RE.search(question)
    if not year_m:
        return None
    yy = year_m.group(1)

    # Month name
    month_m = _MONTH_NAME_RE.search(question)
    if not month_m:
        return yy

    mm = _MONTH_MAP[month_m.group(1).lower()]

    # "dia N"
    day_m = _DAY_RE.search(question)
    if day_m:
        return yy + mm + day_m.group(1).zfill(2)

    return yy + mm

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

# Cache of client names extracted from the tickets collection.
# Populated on first query; lives for the duration of the server process.
_clients_cache: list[str] | None = None


def _get_known_clients() -> list[str]:
    """Return all unique client names found in the tickets collection."""
    global _clients_cache
    if _clients_cache is not None:
        return _clients_cache
    try:
        col    = get_collection(CHROMA_PATH, EMBED_MODEL, TICKETS_COLLECTION)
        total  = col.count()
        clients: set[str] = set()
        offset = 0
        while offset < total:
            data = col.get(include=["documents"], limit=500, offset=offset)
            for doc in data["documents"]:
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

    # Auto-detect ERP codes (e.g. CFAB24, cext24) — normalize to uppercase since
    # stored tickets always use uppercase program codes.
    for match in _ERP_CODE_RE.finditer(question):
        code = match.group().upper()
        if code not in found:
            found.append(code)

    # Auto-detect ticket numbers (e.g. 250922016 → YYMMDD + 3-digit sequence)
    for match in _TICKET_RE.finditer(question):
        ticket = match.group()
        if ticket not in found:
            found.append(ticket)

    # Message codes (KND-XXXXX, ORA-XXXXXX) — match against the Mensagens: field
    for match in _MSG_CODE_RE.finditer(question):
        code = match.group().upper()
        if code not in found:
            found.append(code)

    # Pedido de Serviço — search for the exact "Pedido de Serviço: <value>" substring
    m = _PEDIDO_RE.search(question)
    if m:
        pedido_key = f"Pedido de Serviço: {m.group(1)}"
        if pedido_key not in found:
            found.append(pedido_key)

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
    Hybrid retrieval over two dedicated collections (documents + tickets).

    For each collection:
      dense semantic search  ─┐
                               ├─ RRF fusion → top-N candidates
      BM25 sparse search     ─┘

    Ticket collection also runs exact keyword and date-prefix matches.
    All candidates are merged and reranked by a cross-encoder.

    Returns (context_block, unique_source_names).
    """
    try:
        docs_col    = get_collection(CHROMA_PATH, EMBED_MODEL, DOCS_COLLECTION)
        tickets_col = get_collection(CHROMA_PATH, EMBED_MODEL, TICKETS_COLLECTION)
        docs_count    = docs_col.count()
        tickets_count = tickets_col.count()

        if docs_count + tickets_count == 0:
            return "", []

        # Results keyed by chunk id.  Value: (text, metadata, proximity_pct).
        # proximity_pct is used only for the source-display threshold; the
        # cross-encoder handles the final relevance ordering.
        seen: dict[str, tuple[str, dict, float]] = {}

        # ── 1. Documents: dense + BM25 + RRF ──────────────────────────────
        if docs_count > 0:
            bm25_store.ensure_built(DOCS_COLLECTION, CHROMA_PATH, EMBED_MODEL)

            d_res = docs_col.query(
                query_texts=[question],
                n_results=min(_DOC_RESULTS * 4, docs_count),
                include=["documents", "metadatas", "distances"],
            )
            doc_dense = list(zip(
                d_res["ids"][0], d_res["documents"][0],
                d_res["metadatas"][0], d_res["distances"][0],
            ))
            id_to_prox = {
                cid: max(0.0, (1.0 - dist / 2.0) * 100)
                for cid, _, _, dist in doc_dense
            }

            doc_sparse = bm25_store.search(question, DOCS_COLLECTION, _DOC_RESULTS * 4)
            doc_fused  = bm25_store.rrf_fuse(doc_dense, doc_sparse)

            for cid, doc, meta, _ in doc_fused[:_DOC_RESULTS]:
                seen[cid] = (doc, meta, id_to_prox.get(cid, _MIN_PROXIMITY))

        # ── 2. Tickets: dense + BM25 + RRF ────────────────────────────────
        if tickets_count > 0:
            bm25_store.ensure_built(TICKETS_COLLECTION, CHROMA_PATH, EMBED_MODEL)

            t_res = tickets_col.query(
                query_texts=[question],
                n_results=min(_TICKET_RESULTS * 3, tickets_count),
                include=["documents", "metadatas", "distances"],
            )
            ticket_dense = list(zip(
                t_res["ids"][0], t_res["documents"][0],
                t_res["metadatas"][0], t_res["distances"][0],
            ))
            id_to_prox_t = {
                cid: max(0.0, (1.0 - dist / 2.0) * 100)
                for cid, _, _, dist in ticket_dense
            }

            ticket_sparse = bm25_store.search(question, TICKETS_COLLECTION, _TICKET_RESULTS * 3)
            ticket_fused  = bm25_store.rrf_fuse(ticket_dense, ticket_sparse)

            for cid, doc, meta, _ in ticket_fused[:_TICKET_RESULTS]:
                prox = id_to_prox_t.get(cid, _MIN_PROXIMITY)
                if prox >= _MIN_PROXIMITY:
                    seen[cid] = (doc, meta, prox)

        # ── 3. Chats (learned knowledge): dense search ────────────────────
        try:
            chats_col   = get_collection(CHROMA_PATH, EMBED_MODEL, CHATS_COLLECTION)
            chats_count = chats_col.count()
            if chats_count > 0:
                c_res = chats_col.query(
                    query_texts=[question],
                    n_results=min(_CHATS_RESULTS, chats_count),
                    include=["documents", "metadatas", "distances"],
                )
                for cid, doc, meta, dist in zip(
                    c_res["ids"][0], c_res["documents"][0],
                    c_res["metadatas"][0], c_res["distances"][0],
                ):
                    prox = max(0.0, (1.0 - dist / 2.0) * 100)
                    if prox >= _MIN_PROXIMITY and cid not in seen:
                        seen[cid] = (doc, meta, prox)
        except Exception:
            pass

        # ── 5. Keyword exact-match (tickets) ──────────────────────────────
        if tickets_count > 0:
            keywords = _extract_keywords(question)
            for term in keywords:
                try:
                    kw = tickets_col.get(
                        where_document={"$contains": term},
                        include=["documents", "metadatas"],
                        limit=_KEYWORD_RESULTS,
                    )
                except Exception:
                    continue
                for cid, doc, meta in zip(kw["ids"], kw["documents"], kw["metadatas"]):
                    if cid not in seen:
                        seen[cid] = (doc, meta, 100.0)

        # ── 6. Date-filtered search (tickets) ─────────────────────────────
        if tickets_count > 0:
            date_prefix = _extract_date_prefix(question)
            if date_prefix:
                try:
                    kw = tickets_col.get(
                        where_document={"$contains": "Atendimento: " + date_prefix},
                        include=["documents", "metadatas"],
                        limit=_DATE_RESULTS,
                    )
                    for cid, doc, meta in zip(kw["ids"], kw["documents"], kw["metadatas"]):
                        if cid not in seen:
                            seen[cid] = (doc, meta, 100.0)
                except Exception:
                    pass

        if not seen:
            return "", []

        # ── 7. Cross-encoder rerank ────────────────────────────────────────
        candidates = list(seen.values())
        order  = rerank(question, [c[0] for c in candidates])
        ranked = [candidates[i] for i in order]
        top    = ranked[:_DOC_RESULTS + _TICKET_RESULTS + _KEYWORD_RESULTS + _DATE_RESULTS]

        context = "\n---\n".join(entry[0] for entry in top)
        sources: list[str] = []
        for _, meta, _ in top:
            src = meta.get("source", "unknown")
            if src not in sources:
                sources.append(src)

        return context, sources

    except Exception:
        return "", []


# ── Conversation-aware RAG query ───────────────────────────────────────────

_FOLLOWUP_RE = re.compile(
    r"\b(isso|este|esta|esse|essa|aquele|aquela|ele|ela|eles|elas|"
    r"primeiro|segundo|terceiro|último|anterior|próximo|"
    r"mais detalhes?|explique|continue|e o|e a|e os|e as)\b",
    re.IGNORECASE,
)


def _build_rag_query(question: str, history: list[dict]) -> str:
    """
    For short or pronoun-heavy follow-up questions the current question
    carries too little semantic signal for a useful vector search.

    Strategy:
    - Collect the last 2 prior user questions (captures the topic even when
      there is an intermediate vague question like "qual é o programa?")
    - Append the first 150 chars of the last assistant response, which often
      contains the exact terms the user is referring to with "isso/ele/ela"
    - Combine with the current question (capped at 400 chars total)
    """
    is_followup = len(question.strip()) < 80 or bool(_FOLLOWUP_RE.search(question))
    if not is_followup or not history:
        return question

    prior = history[:-1]  # exclude the current question entry
    prior_user: list[str] = []
    last_assistant: str = ""

    for msg in reversed(prior):
        if msg["role"] == "assistant" and not last_assistant:
            last_assistant = msg["content"].strip()[:150]
        elif msg["role"] == "user" and len(prior_user) < 2:
            prior_user.append(msg["content"].strip())
        if len(prior_user) == 2 and last_assistant:
            break

    parts = list(reversed(prior_user))  # chronological order
    if last_assistant:
        parts.append(last_assistant)
    parts.append(question)
    return " ".join(parts)[:400]


# ── Vision pre-pass ───────────────────────────────────────────────────────

_IMAGE_EXTRACT_PROMPT = (
    "Analise esta imagem e responda SOMENTE com:\n"
    "- Códigos de erro visíveis no formato KND-NNNNN ou ORA-NNNNNN\n"
    "- Códigos de programa ERP visíveis (letras maiúsculas seguidas de dígitos, ex: EPRO15, PEDI1)\n"
    "- Uma frase de até 15 palavras descrevendo o problema mostrado\n\n"
    "Sem explicações adicionais."
)


def _extract_image_terms(images: list[str]) -> str:
    """
    Quick vision pass to pull error codes, program codes, and a brief
    problem description out of the attached images.
    The result is appended to the RAG query so retrieval can find relevant
    tickets even when the user sends an image with no (or minimal) text.

    Uses streaming so the per-token timeout (180 s) resets on each chunk,
    avoiding cold-model-load timeouts that plagued the old stream=False call.
    Returns an empty string on any failure.
    """
    payload = json.dumps({
        "model": VISION_EXTRACT_MODEL,
        "messages": [{"role": "user", "content": _IMAGE_EXTRACT_PROMPT, "images": images}],
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        tokens: list[str] = []
        with urllib.request.urlopen(req, timeout=180) as r:
            for raw_line in r:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    tokens.append(token)
                if chunk.get("done"):
                    break
        return "".join(tokens).strip()
    except Exception as e:
        print(f"[chat] image term extraction failed: {e}", file=sys.stderr)
        return ""


# ── Conversation summarisation (learn feature) ─────────────────────────────

_SUMMARY_PROMPT = (
    "Analise a conversa abaixo entre um consultor de ERP e o assistente ChatKND.\n"
    "Crie um documento de conhecimento objetivo com:\n"
    "1. Tópico ou problema principal abordado\n"
    "2. Códigos de erro, programas ERP, tickets ou dados técnicos mencionados\n"
    "3. Correções feitas pelo consultor a respostas incorretas do assistente\n"
    "4. Solução ou conclusão final\n\n"
    "Escreva de forma direta e factual, como uma base de conhecimento interna.\n"
    "Preserve códigos exatos (KND-XXXXX, ORA-XXXXX, nomes de programas).\n"
    "Não mencione que é um resumo de conversa.\n\n"
    "CONVERSA:\n{conversation}\n\n"
    "DOCUMENTO DE CONHECIMENTO:"
)


def generate_summary(messages: list[dict]) -> str:
    """
    Summarise a conversation into a reusable knowledge document.
    Uses streaming so cold model-load does not trigger a timeout.
    Returns an empty string on any failure.
    """
    lines = []
    for msg in messages:
        role    = "Consultor" if msg["role"] == "user" else "Assistente"
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    if not lines:
        return ""

    prompt  = _SUMMARY_PROMPT.format(conversation="\n".join(lines))
    payload = json.dumps({
        "model":    CHAT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   True,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        tokens: list[str] = []
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw_line in r:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    tokens.append(token)
                if chunk.get("done"):
                    break
        return "".join(tokens).strip()
    except Exception as e:
        print(f"[chat] summary generation failed: {e}", file=sys.stderr)
        return ""


# ── Generation ─────────────────────────────────────────────────────────────

def generate(question: str, history: list[dict], conversation_id: str | None = None, user_id: str | None = None, images: list[str] | None = None) -> dict:
    """
    Retrieve relevant context from ChromaDB, inject it into the current user
    message, then call Ollama with the full conversation history.

    `history` already contains the current question as its last entry
    (the frontend appends it before sending the request).
    `images` is an optional list of base64-encoded image strings to pass to
    vision-capable models alongside the current message.

    Returns {"answer": str, "sources": [str, ...], "conversation_id": str}.
    """
    _ensure_running()

    # When images are attached, do a quick vision pre-pass to extract error
    # codes, program codes, and a brief description so the RAG retrieval can
    # find relevant tickets even when the user sent minimal text.
    if images:
        image_terms = _extract_image_terms(images)
        rag_query = _build_rag_query(f"{question} {image_terms}".strip(), history)
        context, sources = _retrieve_context(rag_query)
    elif question:
        rag_query = _build_rag_query(question, history)
        context, sources = _retrieve_context(rag_query)
    else:
        context, sources = "", []

    if context:
        current_content = (
            "Use os trechos abaixo da base de conhecimento para responder à pergunta. "
            "Se não forem relevantes, responda com seu próprio conhecimento.\n\n"
            f"Contexto:\n{context}\n\n"
            f"Pergunta: {question}"
        )
    else:
        current_content = question or "Descreva o que você vê nessa imagem."

    prior_turns = history[:-1] if history else []
    if len(prior_turns) > _MAX_HISTORY_TURNS:
        prior_turns = prior_turns[-_MAX_HISTORY_TURNS:]

    current_msg: dict = {"role": "user", "content": current_content}
    if images:
        current_msg["images"] = images

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += prior_turns
    messages.append(current_msg)

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
            answer = data["message"]["content"]

            if user_id:
                if not conversation_id:
                    conversation_id = create_conversation(question, user_id)
                append_exchange(conversation_id, user_id, question, answer, sources)

            return {"answer": answer, "sources": sources, "conversation_id": conversation_id}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Ollama returned {e.code}: {body}")


def stream_generate(
    question: str,
    history: list[dict],
    conversation_id: str | None = None,
    user_id: str | None = None,
    images: list[str] | None = None,
):
    """
    Streaming version of generate(). Yields dicts:
      {"token": str}                                           — one per chunk
      {"done": True, "sources": list, "conversation_id": str} — final metadata
      {"error": str}                                           — on failure
    """
    _ensure_running()

    if images:
        image_terms = _extract_image_terms(images)
        rag_query = _build_rag_query(f"{question} {image_terms}".strip(), history)
        context, sources = _retrieve_context(rag_query)
    elif question:
        rag_query = _build_rag_query(question, history)
        context, sources = _retrieve_context(rag_query)
    else:
        context, sources = "", []

    if context:
        current_content = (
            "Use os trechos abaixo da base de conhecimento para responder à pergunta. "
            "Se não forem relevantes, responda com seu próprio conhecimento.\n\n"
            f"Contexto:\n{context}\n\n"
            f"Pergunta: {question}"
        )
    else:
        current_content = question or "Descreva o que você vê nessa imagem."

    prior_turns = history[:-1] if history else []
    if len(prior_turns) > _MAX_HISTORY_TURNS:
        prior_turns = prior_turns[-_MAX_HISTORY_TURNS:]

    current_msg: dict = {"role": "user", "content": current_content}
    if images:
        current_msg["images"] = images

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += prior_turns
    messages.append(current_msg)

    payload = json.dumps({
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    full_answer: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw_line in r:
                line = raw_line.strip()
                if not line:
                    continue
                data = json.loads(line)
                token = data.get("message", {}).get("content", "")
                if token:
                    full_answer.append(token)
                    yield {"token": token}
                if data.get("done"):
                    break
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        yield {"error": f"Ollama returned {e.code}: {body}"}
        return
    except Exception as e:
        yield {"error": str(e)}
        return

    answer = "".join(full_answer)
    if user_id:
        if not conversation_id:
            conversation_id = create_conversation(question or answer[:60], user_id)
        append_exchange(conversation_id, user_id, question, answer, sources)

    yield {"done": True, "sources": sources, "conversation_id": conversation_id}
