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
import base64
import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from tqdm import tqdm
from html.parser import HTMLParser
from pathlib import Path

import oracledb
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "doc_reader"))

from chroma_store import get_collection, upsert_many  # noqa: E402
from cleaner import strip_greetings                    # noqa: E402

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

MAX_CONV_CHARS      = 6000   # truncate conversation sent to LLM (keeps prompt manageable)
LLM_TIMEOUT         = 180    # seconds — large model on first ticket can be slow
IMAGE_FETCH_TIMEOUT = 15     # seconds per image download
_COMMIT_EVERY       = 10     # commit pending summaries to ChromaDB every N tickets processed

_IMAGE_HOST = "https://kundencloud.com.br:3826"
_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

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
      --AND A.ID = 260102008
      AND A.DATA_INICIO BETWEEN TO_DATE('01/01/2025', 'DD/MM/RRRR') AND TO_DATE('31/01/2026', 'DD/MM/RRRR')
    ORDER BY M.ATENDIMENTO_ID, M.DATA_HORA
"""

# Keep WHERE clause identical to _QUERY (minus ORDER BY) so the count is accurate.
_COUNT_QUERY = """
    SELECT COUNT(DISTINCT M.ATENDIMENTO_ID)
    FROM PESSOA_CRM P,
         CLIENTE_CRM C,
         ATENDIMENTO_CRM A,
         MSG_ATENDIMENTO_CRM M
    WHERE A.ID = M.ATENDIMENTO_ID
      AND C.ID = A.CLIENTE_ID
      AND P.ID = C.PESSOA_ID
      --AND A.ID = 260102008
      AND A.DATA_INICIO BETWEEN TO_DATE('01/01/2025', 'DD/MM/RRRR') AND TO_DATE('31/01/2026', 'DD/MM/RRRR')
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


# ── Image helpers ──────────────────────────────────────────────────────────

def _extract_image_urls(texto: str) -> list[str]:
    """Return a list of absolute image URLs found in an HTML TEXTO field.

    texto from Oracle arrives as a JSON-encoded value — either a JSON string
    (the HTML message body) or a JSON array (a status-change event with no HTML).
    We parse it first to recover the actual HTML before running the img regex.
    """
    if not texto:
        return []
    try:
        parsed = json.loads(texto)
        if isinstance(parsed, str):
            raw = parsed          # unwrap the JSON string → actual HTML
        else:
            return []             # status-change array/object, no HTML content
    except Exception:
        raw = texto               # not JSON, use as-is
    raw = html_lib.unescape(raw)
    urls = []
    for m in _IMG_SRC_RE.finditer(raw):
        src = m.group(1).replace("%HOST%", _IMAGE_HOST)
        if not src.startswith("http"):
            src = _IMAGE_HOST + "/" + src.lstrip("/")
        urls.append(src)
    return urls


def _fetch_images(urls: list[str]) -> list[str]:
    """Download images in parallel and return them as base64 strings; silently skip failures."""
    if not urls:
        return []

    def _download(url: str) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=IMAGE_FETCH_TIMEOUT) as r:
                return base64.b64encode(r.read()).decode()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as executor:
        results = list(executor.map(_download, urls))
    return [r for r in results if r is not None]


# ── Conversation builder ───────────────────────────────────────────────────

def _decode_texto(texto: str) -> str:
    """Unwrap the JSON-encoded HTML string that Oracle stores in TEXTO."""
    if not texto:
        return ""
    try:
        parsed = json.loads(texto)
        return parsed if isinstance(parsed, str) else ""
    except Exception:
        return texto


def _build_conversation(messages: list[dict]) -> str:
    """Clean and concatenate all messages into a readable conversation block."""
    parts = []
    for msg in messages:
        body = strip_html(_decode_texto(msg["texto"] or ""))
        body = strip_greetings(body).strip()
        if body:
            parts.append(f"[{msg['data_hora']} | {msg['usuario_id']}]\n{body}")
    return "\n\n".join(parts)


def _first_last_fallback(atendimento_id, cliente: str, pedido_servico: str, messages: list[dict]) -> str:
    """Minimal fallback when LLM summarisation fails."""
    texts = []
    for msg in messages:
        t = strip_greetings(strip_html(_decode_texto(msg["texto"] or ""))).strip()
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
Analise a conversa abaixo e responda EXATAMENTE neste formato, sem nenhum texto adicional.
Se forem fornecidas imagens, use-as como contexto adicional para entender melhor o problema.
Nas imagens, procure por códigos de mensagem de erro (ex: KND-004678, ORA-00942). Somente quando uma imagem contiver um código de mensagem de erro, verifique também se há um código de programa ERP visível nessa mesma imagem (ex: EPRO15, PEDI1, ESTO7 — letras maiúsculas seguidas de dígitos) e inclua-o no campo Programa. Imagens sem código de erro devem ser ignoradas para fins de extração de programa.

Cliente: {cliente}
Pedido de Serviço: {pedido_servico}
Programa: [programa(s) ERP mencionado(s) no texto ou visíveis nas imagens, ex: CFAB24, EPRO15 — ou "Não especificado"]
Problema: [descrição objetiva do problema em 1 a 3 frases]
Solução: [descrição objetiva da solução aplicada em 1 a 3 frases — ou "Não resolvido" se o ticket não tiver solução]
Mensagens: [lista separada por vírgulas de todos os códigos de mensagem encontrados no texto ou nas imagens, no formato KND-NNNNN ou ORA-NNNNNN — apenas os códigos, sem o texto da mensagem — ou "Nenhum" se não houver]
Atendimento: {atendimento_id}

CONVERSA:
{conversation}
"""

def _summarize(atendimento_id, cliente: str, pedido_servico: str, conversation: str, images: list[str] | None = None) -> str | None:
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
    payload_dict: dict = {
        "model": SUMMARIZE_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if images:
        payload_dict["images"] = images
    payload = json.dumps(payload_dict).encode()
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

    # Load all existing ticket IDs into a set for O(1) duplicate checks.
    # This single pass replaces a per-ticket col.get() that would scan
    # the full collection on every call — critical at thousands of tickets.
    print("Loading existing ticket IDs from ChromaDB…", end=" ", flush=True)
    known_ids: set[str] = set()
    offset = 0
    while True:
        data = col.get(include=["metadatas"], limit=1000, offset=offset)
        if not data["ids"]:
            break
        for meta in data["metadatas"]:
            src = meta.get("source", "")
            if src.startswith("ticket_"):
                known_ids.add(src)
        offset += len(data["ids"])
        if len(data["ids"]) < 1000:
            break
    print(f"{len(known_ids)} existing tickets found.")

    if reset and known_ids:
        print(f"Wiping {len(known_ids)} existing ticket chunks…", end=" ", flush=True)
        src_list = list(known_ids)
        # Delete in batches of 500 using $in to avoid one delete call per source.
        for i in range(0, len(src_list), 500):
            col.delete(where={"source": {"$in": src_list[i : i + 500]}})
        known_ids.clear()
        print("done.")

    conn    = _connect()
    cursor  = conn.cursor()
    cursor.arraysize = 500   # fetch 500 rows per network round-trip (was 200)

    count_cur = conn.cursor()
    count_cur.execute(_COUNT_QUERY)
    total_tickets = count_cur.fetchone()[0]
    count_cur.close()
    if limit:
        total_tickets = min(total_tickets, limit)

    cursor.execute(_QUERY)

    current_id    = None
    messages: list[dict] = []
    tickets_done  = 0
    skipped       = 0
    already_done  = 0
    pending: list[tuple[str, str]] = []  # (source, summary) waiting for batch upsert

    def _commit_batch() -> None:
        if pending:
            upsert_many(col, pending)
            pending.clear()

    def _flush(atendimento_id, msgs: list[dict]) -> str | bool | None:
        """
        Returns str   = new summary ready to upsert (caller handles batching),
                False = skipped (empty content),
                None  = already in ChromaDB, not reprocessed.
        """
        source = f"ticket_{atendimento_id}"
        if source in known_ids:
            return None

        images: list[str] = []
        if use_llm:
            all_urls: list[str] = []
            for msg in msgs:
                all_urls.extend(_extract_image_urls(msg.get("texto") or ""))
            images = _fetch_images(all_urls)  # parallel download

        conversation = _build_conversation(msgs)
        if not conversation.strip():
            return False

        cliente        = msgs[0].get("cliente", "") if msgs else ""
        pedido_servico = msgs[0].get("pedido_servico", "") if msgs else ""

        if use_llm:
            summary = _summarize(atendimento_id, cliente, pedido_servico, conversation, images)
            if not summary:
                summary = _first_last_fallback(atendimento_id, cliente, pedido_servico, msgs)
        else:
            summary = _first_last_fallback(atendimento_id, cliente, pedido_servico, msgs)

        if not summary.strip():
            return False

        return summary

    print(f"Streaming tickets from Oracle  (LLM summarisation: {'on' if use_llm else 'off'})…\n")

    bar = tqdm(
        total=total_tickets,
        unit="ticket",
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
    )

    def _update_bar(atendimento_id) -> None:
        bar.set_description(f"#{atendimento_id}")
        bar.set_postfix(new=tickets_done, done=already_done, skip=skipped)
        bar.update(1)

    for row in cursor:
        atendimento_id, data_hora, usuario_id, texto, cliente, pedido_servico = row

        if current_id is None:
            current_id = atendimento_id

        if atendimento_id != current_id:
            bar.set_description(f"#{current_id}")
            result = _flush(current_id, messages)
            if result is None:
                already_done += 1
            elif result is False:
                skipped += 1
            else:
                source = f"ticket_{current_id}"
                pending.append((source, result))
                known_ids.add(source)
                tickets_done += 1

            total_processed = tickets_done + already_done + skipped
            if total_processed % _COMMIT_EVERY == 0:
                _commit_batch()

            _update_bar(current_id)

            if limit and total_processed >= limit:
                current_id = None
                messages   = []
                break
            current_id = atendimento_id
            messages   = []

        messages.append({
            "data_hora":      str(data_hora),
            "usuario_id":     str(usuario_id or ""),
            "texto":          texto,
            "cliente":        str(cliente or ""),
            "pedido_servico": str(pedido_servico or ""),
        })

    if current_id is not None and messages:
        bar.set_description(f"#{current_id}")
        result = _flush(current_id, messages)
        if result is None:
            already_done += 1
        elif result is False:
            skipped += 1
        else:
            source = f"ticket_{current_id}"
            pending.append((source, result))
            known_ids.add(source)
            tickets_done += 1
        _update_bar(current_id)

    bar.close()
    _commit_batch()  # flush any remaining summaries

    cursor.close()
    conn.close()

    print(f"\nDone.  {tickets_done} tickets summarised → {tickets_done} chunks  |  {already_done} already done  |  {skipped} skipped (empty).")


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
