"""
Standalone script — ingest support tickets from Oracle into ChromaDB.

Each ticket is summarised by a local LLM (Ollama) into a compact structured
chunk before being stored.  This removes conversational noise and produces
embeddings that retrieve well against "find me similar problems" queries.

Usage:
    python ticket_ingest/ingest.py              # ingest all tickets
    python ticket_ingest/ingest.py --limit 50   # first 50 tickets (testing)
    python ticket_ingest/ingest.py --reset       # wipe ticket chunks then ingest
    python ticket_ingest/ingest.py --no-llm      # skip LLM, use first+last fallback
"""

import argparse
import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import oracledb
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "doc_reader"))

from chroma_store import delete_chunks, get_collection, upsert_chunks  # noqa: E402
from cleaner import strip_greetings                                     # noqa: E402

# ── Config ─────────────────────────────────────────────────────────────────

ORACLE_USER        = os.getenv("ORACLE_USER", "")
ORACLE_PASSWORD    = os.getenv("ORACLE_PASSWORD", "")
ORACLE_DSN         = os.getenv("ORACLE_DSN", "")
ORACLE_CLIENT_PATH = os.getenv("ORACLE_CLIENT_PATH", "").strip()
CHROMA_PATH        = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL        = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
COLLECTION_NAME    = os.getenv("CHROMA_COLLECTION", "documents")
OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
SUMMARIZE_MODEL    = os.getenv("SUMMARIZE_MODEL") or os.getenv("CHAT_MODEL", "gemma4:31b-cloud")

MAX_CONV_CHARS = 6000   # truncate conversation sent to LLM (keeps prompt manageable)
LLM_TIMEOUT    = 180    # seconds — large model on first ticket can be slow

if ORACLE_CLIENT_PATH:
    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)

# ── Oracle query ───────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        M.ATENDIMENTO_ID,
        M.DATA_HORA,
        M.USUARIO_ID,
        M.TEXTO,
        P.NOME CLIENTE,
        A.PEDIDO PEDIDO_SERVICO
    FROM PESSOA_CRM P,
         CLIENTE_CRM C,
         ATENDIMENTO_CRM A,
         MSG_ATENDIMENTO_CRM M
    WHERE A.ID = M.ATENDIMENTO_ID
      AND C.ID = A.CLIENTE_ID
      AND P.ID = C.PESSOA_ID
      AND A.DATA_INICIO BETWEEN TO_DATE('01/01/2026', 'DD/MM/RRRR') AND TO_DATE('31/01/2026', 'DD/MM/RRRR')
    ORDER BY M.ATENDIMENTO_ID, M.DATA_HORA
"""

# ── HTML stripping ─────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    _SKIP_TAGS = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        if tag in {"p", "div", "li", "tr"}:
            self._parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html_lib.unescape(raw)
    stripper = _HTMLStripper()
    try:
        stripper.feed(text)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+", " ", stripper.get_text()).strip()


# ── Conversation builder ───────────────────────────────────────────────────

def _build_conversation(messages: list[dict]) -> str:
    """Clean and concatenate all messages into a readable conversation block."""
    parts = []
    for msg in messages:
        body = strip_html(msg["texto"] or "")
        body = strip_greetings(body).strip()
        if body:
            parts.append(f"[{msg['data_hora']} | {msg['usuario_id']}]\n{body}")
    return "\n\n".join(parts)


def _first_last_fallback(atendimento_id, cliente: str, pedido_servico: str, messages: list[dict]) -> str:
    """Minimal fallback when LLM summarisation fails."""
    texts = []
    for msg in messages:
        t = strip_greetings(strip_html(msg["texto"] or "")).strip()
        if t:
            texts.append(t)
    if not texts:
        return ""
    problem    = texts[0]
    resolution = texts[-1] if len(texts) > 1 else ""
    lines = [
        f"Cliente: {cliente}",
        f"Pedido de Serviço: {pedido_servico or 'Não informado'}",
        f"Atendimento: {atendimento_id}",
    ]
    lines.append(f"Problema: {problem[:400]}")
    if resolution:
        lines.append(f"Resolução: {resolution[:400]}")
    return "\n".join(lines)


# ── Ollama helpers ─────────────────────────────────────────────────────────

def _ollama_is_running() -> bool:
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
        if _ollama_is_running():
            return
    raise RuntimeError("Ollama did not start within 15 seconds.")


def _ensure_ollama() -> None:
    if not _ollama_is_running():
        print("  Starting Ollama…")
        _start_ollama()


_SUMMARY_PROMPT = """\
Você é um assistente que resume tickets de suporte de ERP Oracle Forms.
Analise a conversa abaixo e responda EXATAMENTE neste formato, sem nenhum texto adicional:

Cliente: {cliente}
Pedido de Serviço: {pedido_servico}
Programa: [programa(s) ERP mencionado(s), ex: CFAB24, EPRO15 — ou "Não especificado"]
Problema: [descrição objetiva do problema em 1 a 3 frases]
Solução: [descrição objetiva da solução aplicada em 1 a 3 frases — ou "Não resolvido" se o ticket não tiver solução]
Atendimento: {atendimento_id}

CONVERSA:
{conversation}
"""

def _summarize(atendimento_id, cliente: str, pedido_servico: str, conversation: str) -> str | None:
    """
    Call Ollama to summarise a ticket conversation.
    Returns the structured summary string, or None if the call fails.
    """
    truncated = conversation[:MAX_CONV_CHARS]
    prompt = _SUMMARY_PROMPT.format(
        atendimento_id=atendimento_id,
        cliente=cliente,
        pedido_servico=pedido_servico or "Não informado",
        conversation=truncated,
    )
    payload = json.dumps({
        "model": SUMMARIZE_MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            data = json.loads(r.read())
            text = data.get("response", "").strip()
            # Validate: the summary must contain at least "Problema:" and "Solução:"
            if "Problema:" in text and "Solução:" in text:
                return text
            return None
    except Exception:
        return None


# ── Oracle connection ──────────────────────────────────────────────────────

def _clob_output_handler(cursor, name, default_type, size, precision, scale):
    if default_type == oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)


def _connect():
    if not (ORACLE_USER and ORACLE_PASSWORD and ORACLE_DSN):
        sys.exit("ERROR: ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN must be set in .env")
    conn = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )
    conn.outputtypehandler = _clob_output_handler
    return conn


# ── Ingestion logic ────────────────────────────────────────────────────────

def ingest_tickets(
    limit: int | None = None,
    reset: bool = False,
    use_llm: bool = True,
) -> None:

    if use_llm:
        _ensure_ollama()

    col = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)

    if reset:
        print("Wiping existing ticket chunks…")
        total = col.count()
        if total:
            ticket_sources: set[str] = set()
            offset = 0
            while offset < total:
                data = col.get(include=["metadatas"], limit=500, offset=offset)
                for meta in data["metadatas"]:
                    src = meta.get("source", "")
                    if src.startswith("ticket_"):
                        ticket_sources.add(src)
                offset += 500
            for src in ticket_sources:
                delete_chunks(col, src)
            print(f"  Removed chunks for {len(ticket_sources)} tickets.")

    conn    = _connect()
    cursor  = conn.cursor()
    cursor.arraysize = 200
    cursor.execute(_QUERY)

    current_id    = None
    messages: list[dict] = []
    tickets_done  = 0
    skipped       = 0

    def _flush(atendimento_id, msgs: list[dict]) -> bool:
        source       = f"ticket_{atendimento_id}"
        conversation = _build_conversation(msgs)
        if not conversation.strip():
            return False

        cliente        = msgs[0].get("cliente", "") if msgs else ""
        pedido_servico = msgs[0].get("pedido_servico", "") if msgs else ""

        if use_llm:
            summary = _summarize(atendimento_id, cliente, pedido_servico, conversation)
            if not summary:
                summary = _first_last_fallback(atendimento_id, cliente, pedido_servico, msgs)
        else:
            summary = _first_last_fallback(atendimento_id, cliente, pedido_servico, msgs)

        if not summary.strip():
            return False

        delete_chunks(col, source)
        upsert_chunks(col, source, [summary])   # one chunk per ticket
        return True

    print(f"Streaming tickets from Oracle  (LLM summarisation: {'on' if use_llm else 'off'})…\n")

    for row in cursor:
        atendimento_id, data_hora, usuario_id, texto, cliente, pedido_servico = row

        if current_id is None:
            current_id = atendimento_id

        if atendimento_id != current_id:
            ok = _flush(current_id, messages)
            if ok:
                tickets_done += 1
            else:
                skipped += 1
            if (tickets_done + skipped) % 10 == 0:
                print(f"  {tickets_done} summarised  |  {skipped} skipped (empty)…")
            if limit and tickets_done >= limit:
                current_id = None
                messages   = []
                break
            current_id = atendimento_id
            messages   = []

        messages.append({
            "data_hora":     str(data_hora),
            "usuario_id":    str(usuario_id or ""),
            "texto":         texto,
            "cliente":       str(cliente or ""),
            "pedido_servico": str(pedido_servico or ""),
        })

    if current_id is not None and messages:
        ok = _flush(current_id, messages)
        if ok:
            tickets_done += 1
        else:
            skipped += 1

    cursor.close()
    conn.close()

    print(f"\nDone.  {tickets_done} tickets summarised → {tickets_done} chunks  |  {skipped} skipped.")


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Oracle support tickets into ChromaDB via LLM summarisation.")
    parser.add_argument("--limit",  type=int,        default=None,  help="Stop after N tickets (testing).")
    parser.add_argument("--reset",  action="store_true",            help="Delete existing ticket chunks before ingesting.")
    parser.add_argument("--no-llm", action="store_true",            help="Skip LLM; use first+last-message fallback.")
    args = parser.parse_args()

    ingest_tickets(
        limit=args.limit,
        reset=args.reset,
        use_llm=not args.no_llm,
    )
