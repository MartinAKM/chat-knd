# Chat-KND

RAG-based knowledge assistant for Kunden Systems ERP consultants. Consultants receive Oracle Forms ERP support tickets and need fast answers from historical solutions and documentation.

## How it works

1. ERP support documents (PDF, DOCX, TXT, MD) are ingested into a local ChromaDB vector store
2. Each document is cleaned, stripped of irrelevant metadata (ROTINA/ACESSO blocks), and split into chunks
3. Chunks are embedded with `sentence-transformers/all-MiniLM-L6-v2` (runs locally, no external service required) and stored in ChromaDB
4. The viewer lets you browse, search, and semantically query the stored knowledge

## Stack

| Layer | Technology |
|---|---|
| Vector store | ChromaDB (local persistent) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (local, no GPU required) |
| LLM (planned) | `llama3` via Ollama |
| Database | Oracle 12c via `oracledb` thin mode |
| API (planned) | Python + FastAPI |

## Requirements

- Python 3.10+
- Oracle 12c access (for ticket ingestion, optional)

No external embedding service needed — `sentence-transformers` runs fully locally and downloads the model (~90 MB) from HuggingFace on first use.

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / Mac

# 2. Install dependencies
pip install -r doc_reader/requirements.txt

# 3. Copy and fill in the environment file
copy .env.example .env       # Windows
# cp .env.example .env       # Linux / Mac
```

**.env variables:**

| Variable | Purpose |
|---|---|
| `ORACLE_USER` / `ORACLE_PASSWORD` / `ORACLE_DSN` | Oracle connection |
| `ORACLE_CALLS_QUERY` | SELECT query to fetch support tickets |
| `CHROMA_PATH` | ChromaDB persistence directory (default: `chroma_data`) |
| `CHROMA_COLLECTION` | Collection name (default: `documents`) |
| `OLLAMA_BASE_URL` | Ollama endpoint for LLM generation (default: `http://localhost:11434`) |
| `EMBED_MODEL` | Embedding model (default: `all-MiniLM-L6-v2`) |
| `VIEWER_PORT` | Viewer port (default: `8001`) |

## Ingesting documents

Place your documents inside any folder and run:

```bash
cd doc_reader

# Ingest a single file
python ingest.py path/to/document.pdf

# Ingest an entire folder
python ingest.py path/to/documents/

# Delete all chunks from a specific file
python ingest.py --delete document.pdf
```

Supported formats: `.pdf`, `.docx`, `.txt`, `.md`

The pipeline automatically:
- Extracts text from the file
- Strips ERP navigation metadata (ROTINA / BASE / ACESSO / LOGIN / SENHA blocks)
- Removes junk lines, excessive whitespace, and control characters
- Splits into overlapping chunks (800 chars, 100 overlap)
- Drops low-quality chunks (too short or mostly non-alphanumeric)
- Embeds and stores in ChromaDB

## ChromaDB Viewer

A browser-based UI to inspect, search, and manage the vector store.

```bash
cd doc_reader
python viewer.py
# Open http://localhost:8001
```

### Features

**Browse** — paginate through all stored chunks, or click a source file in the sidebar to see only its chunks.

**Text search** — keyword match inside chunk content (fast, no Ollama needed).

**Semantic search** — enter a phrase and find chunks by meaning using vector similarity. Results show a proximity percentage; only chunks above **75%** are displayed.

| Badge colour | Proximity |
|---|---|
| Green | ≥ 85% |
| Blue | 80 – 84% |
| Amber | 75 – 79% |

**Upload & Ingest** — drag or pick one or more files directly in the sidebar. The full ingestion pipeline runs server-side and the source list refreshes automatically.

**Delete** — hover any source in the sidebar and click **✕** to remove all its chunks from the store.

**Reset Collection** — button in the stats bar. Drops and recreates the ChromaDB collection with the current embedding model. Required after changing `EMBED_MODEL`; all documents must be re-ingested afterwards.

## Project structure

```
chat-knd/
├── doc_reader/
│   ├── reader.py        # Text extraction (PDF, DOCX, TXT, MD)
│   ├── cleaner.py       # Text normalisation and ROTINA block removal
│   ├── chunker.py       # Overlapping chunk splitter
│   ├── chroma_store.py  # ChromaDB read/write helpers
│   ├── ingest.py        # CLI ingestion entry point
│   ├── viewer.py        # Browser-based ChromaDB viewer
│   └── requirements.txt
├── CLAUDE.md            # Guidance for Claude Code
├── .env                 # Secrets (never committed)
└── .gitignore
```
