# ChatKND

RAG-based AI assistant for Kunden Systems ERP consultants. Consultants receive Oracle Forms ERP support tickets and need fast answers from historical solutions and documentation.

## User roles

| Role | Access |
|---|---|
| `admin` | Full access — Document Viewer, Chat, all API endpoints |
| `user` | Chat only |

The first account created automatically becomes `admin`. All subsequent accounts are `user` by default. Admins can promote or demote users via the **Usuários** button in the Document Viewer header. Users can reset their password via an email link (requires SMTP env vars).

## How it works

1. ERP documents (PDF, DOCX, TXT, MD) and Oracle support tickets are ingested into a local ChromaDB vector store
2. Documents are cleaned, chunked, and embedded with `sentence-transformers/all-MiniLM-L6-v2` (runs fully locally)
3. Embedded images in documents (screenshots, error dialogs) are described by a vision-capable LLM and injected inline so their content is searchable
4. Support tickets are summarised by a local LLM (Ollama) into structured chunks before storage
5. The Chat interface answers questions using hybrid retrieval (semantic + BM25 keyword) and streams a response from Ollama
6. The Document Viewer lets you browse, search, and manage the stored knowledge base

## Stack

| Layer | Technology |
|---|---|
| Vector store | ChromaDB (local persistent) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (local, ~90 MB on first use) |
| LLM | `gemma4:31b-cloud` via Ollama (local) |
| Database | Oracle 12c via `oracledb` |
| Web server | Python stdlib `http.server` |

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) installed and the chat model pulled (`ollama pull gemma4:31b-cloud`)
- Oracle 12c access (only required for ticket ingestion)

## Setup

```bash
# 1. Create and activate a virtual environment using python 3.13 version
py -3.13 -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the environment template and fill in your values
copy .env.example .env       # Windows
# cp .env.example .env       # Linux / Mac
```

Edit `.env` with your Oracle credentials and any model overrides. See `.env.example` for all available variables and their descriptions.

## Running the app

```bash
python app_front/viewer.py
# Open http://localhost:8001
```

The server starts Ollama automatically if it is not already running.

## Chat interface

Navigate to **Chat** in the top nav (or go to `http://localhost:8001/chat`).

- Type a question and press **Enter** (Shift+Enter for a newline)
- The assistant searches the knowledge base via hybrid retrieval and answers in Brazilian Portuguese
- Source documents used as context are shown below each answer as pills
- Conversation history is persisted per-user in `chat_history/<user_id>/` and shown in the sidebar
- Click **New Chat** in the sidebar to start a new conversation, or click a past conversation to resume it
- The assistant uses recent exchanges as context, so follow-up questions like "temos atendimentos sobre isso?" resolve correctly

## Document Viewer

Navigate to **Document Viewer** in the top nav (or go to `http://localhost:8001/`).

**Browse** — paginate through all stored chunks, or click a source file in the sidebar to filter by it.

**Text search** — keyword match inside chunk content (no Ollama required).

**Semantic search** — find chunks by meaning using vector similarity. Only chunks above **60%** proximity are shown.

**Upload & Ingest** — drag or pick one or more files in the sidebar. The full ingestion pipeline runs server-side, including a vision pass for embedded images (requires a vision-capable model via `SUMMARIZE_MODEL` or `CHAT_MODEL`). The source list refreshes automatically.

**Delete** — hover a source in the sidebar and click **✕** to remove all its chunks.

**Clear All Chunks** — removes every chunk from the collection while keeping the collection itself.

**Reset Collection** — drops and recreates the ChromaDB collection. Required after changing `EMBED_MODEL`; all documents must be re-ingested afterwards.

## Ingesting documents

```bash
# Ingest a single file
python doc_reader/ingest.py path/to/document.pdf

# Ingest an entire folder
python doc_reader/ingest.py path/to/documents/

# Delete all chunks for a specific file
python doc_reader/ingest.py --delete document.pdf
```

Supported formats: `.pdf`, `.docx`, `.txt`, `.md`

The pipeline automatically cleans text, strips ERP navigation metadata (ROTINA / BASE / ACESSO blocks), removes greetings and junk lines, splits into overlapping chunks (800 chars, 100 overlap), and drops low-quality chunks before embedding.

Images embedded in PDF and DOCX files are described by the vision model and injected inline at the image position so that error codes, highlighted fields, and screenshots become part of the searchable text. Requires Ollama running with a vision-capable model set in `SUMMARIZE_MODEL` or `CHAT_MODEL`.

## Ingesting Oracle support tickets

```bash
# Ingest all tickets (LLM summarisation enabled by default)
python ticket_ingest/ingest.py

# Test run — process only the first 50 tickets
python ticket_ingest/ingest.py --limit 50

# Wipe existing ticket chunks, then re-ingest
python ticket_ingest/ingest.py --reset

# Skip LLM — use a fast first+last-message fallback instead
python ticket_ingest/ingest.py --no-llm
```

Each ticket is summarised by the LLM into a structured chunk:

```
Programa: CFAB24
Problema: Erro ao emitir NF-e no programa CFAB24 ...
Solução: Atualização do certificado digital resolveu o problema.
Atendimento: 12345
```

This produces embeddings that retrieve well against "find me similar problems" queries. The `SUMMARIZE_MODEL` variable in `.env` lets you use a smaller, faster model for ingestion without changing the chat model.

## Keyword search configuration

Edit `keywords.json` at the project root to configure terms that always trigger exact-match retrieval, regardless of semantic similarity:

```json
{
  "programs": ["CFAB24", "EPRO15"],
  "terms": ["NF-e", "CT-e", "NFS-e"]
}
```

ERP program codes (e.g. `CFAB24`, `EPRO15`) are also auto-detected from the user's question via regex and added to keyword search automatically. Detection is case-insensitive — `cext24` and `CEXT24` retrieve the same results.

## Project structure

```
ChatKND/
├── app_front/
│   ├── viewer.html         # Document Viewer markup
│   ├── viewer.css          # Document Viewer styles
│   ├── viewer.js           # Document Viewer client logic
│   ├── chat.html           # Chat interface markup
│   ├── chat.css            # Chat interface styles
│   ├── chat.js             # Chat interface client logic
│   ├── login.html          # Login page
│   ├── signup.html         # Sign-up page
│   ├── reset_password.html # Password reset request page
│   ├── set_password.html   # New password form (token from email)
│   ├── auth.css            # Shared auth page styles
│   └── viewer.py           # HTTP server — serves static files and /api/* routes
├── auth/
│   └── db.py               # SQLite auth (users, sessions, password reset tokens)
├── chat_api/
│   ├── chat.py             # Hybrid RAG retrieval + Ollama generation
│   └── history.py          # Per-user persistent conversation history
├── doc_reader/
│   ├── reader.py        # Text extraction (PDF, DOCX, TXT, MD)
│   ├── cleaner.py       # Text normalisation and block removal
│   ├── chunker.py       # Overlapping chunk splitter
│   ├── chroma_store.py  # ChromaDB read/write helpers
│   └── ingest.py        # CLI document ingestion
├── ticket_ingest/
│   └── ingest.py        # Oracle ticket ingestion with LLM summarisation
├── keywords.json         # Configured ERP terms for keyword search
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
└── CLAUDE.md             # Guidance for Claude Code
```

## Environment variables

See `.env.example` for the full list with descriptions. Key variables:

| Variable | Purpose |
|---|---|
| `ORACLE_USER` / `ORACLE_PASSWORD` / `ORACLE_DSN` | Oracle connection |
| `ORACLE_CLIENT_PATH` | Path to Oracle Instant Client (optional, for thick mode) |
| `CHROMA_PATH` | ChromaDB persistence directory (default: `chroma_data`) |
| `CHROMA_COLLECTION` | Collection name (default: `documents`) |
| `OLLAMA_BASE_URL` | Ollama endpoint (default: `http://localhost:11434`) |
| `EMBED_MODEL` | Embedding model (default: `all-MiniLM-L6-v2`) |
| `CHAT_MODEL` | LLM for chat (default: `gemma4:31b-cloud`) |
| `SUMMARIZE_MODEL` | LLM for ticket summarisation (defaults to `CHAT_MODEL`) |
| `VIEWER_PORT` | Web server port (default: `8001`) |
| `CHAT_HISTORY_DIR` | Directory for per-user chat history (default: `chat_history/`) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | SMTP server for password reset emails |
| `SMTP_FROM` | Sender address for password reset emails |
| `APP_URL` | Fallback base URL for reset email links; the server uses the HTTP `Host` header when available |

`.env` is never committed. Copy `.env.example` to get started.
