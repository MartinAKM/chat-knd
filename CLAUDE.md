# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

ChatKND is a RAG-based AI assistant for Kunden Systems ERP consultants. Consultants receive Oracle Forms ERP support tickets and need fast answers from historical solutions and documentation.

## Stack

- **API**: Python + FastAPI on port 8000
- **Vector store**: ChromaDB (local persistent) — used because Oracle 12c has no native vector type
- **LLM / embeddings**: Ollama (`llama3` for generation, `nomic-embed-text` for embeddings)
- **Database**: Oracle 12c via `oracledb` thin mode (no thick/instant-client required)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn main:app --reload --port 8000

# Run tests
pytest

# Run a single test
pytest tests/test_foo.py::test_bar -v
```

## Architecture

RAG pipeline flow:
1. User question → embed with `nomic-embed-text` via Ollama
2. ChromaDB similarity search over indexed tickets + documents
3. Top-k context chunks → prompt → `llama3` via Ollama → answer

**Data sources indexed into ChromaDB:**
- Historical support tickets from Oracle (`SUPPORT_TICKETS` table, query configured via `ORACLE_CALLS_QUERY` in `.env`)
- ERP documentation files (PDF, DOCX, TXT, MD) placed in `data/documents/`

## Oracle 12c constraints

- No vector column type (hence ChromaDB for embeddings)
- No `JSON_VALUE` / `JSON_OBJECT` functions (added in 21c)
- CLOB columns (`DESCRIPTION`, `SOLUTION`) require LOB-safe reads — avoid large in-memory string concatenation
- Use `oracledb` thin mode; do not assume Oracle Instant Client is installed

## Environment

All secrets and connection strings go in `.env` (never committed). Key variables:

| Variable | Purpose |
|---|---|
| `ORACLE_USER` / `ORACLE_PASSWORD` / `ORACLE_DSN` | Oracle connection |
| `ORACLE_CALLS_QUERY` | SELECT query to fetch support tickets |
| `CHROMA_PATH` | Local ChromaDB persistence directory |
| `OLLAMA_BASE_URL` | Ollama API endpoint (default `http://localhost:11434`) |
