import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from chunker import chunk_text
from chroma_store import delete_chunks, get_collection, upsert_chunks
from cleaner import clean_text, is_good_chunk, strip_rotina_block
from reader import SUPPORTED_EXTENSIONS, extract_text

load_dotenv()

CHROMA_PATH = os.getenv("CHROMA_PATH", "chroma_data")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.getenv("CHROMA_COLLECTION", "documents")


def process_file(path: Path, collection) -> None:
    print(f"Processing {path.name}...")
    raw = extract_text(path)
    text = clean_text(strip_rotina_block(raw))
    chunks = [c for c in chunk_text(text) if is_good_chunk(c)]
    upsert_chunks(collection, path.name, chunks)
    print(f"  -> {len(chunks)} chunks stored")


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print("Usage:")
        print("  python ingest.py <file_or_directory>          ingest file(s)")
        print("  python ingest.py --delete <filename>          delete chunks by source name")
        sys.exit(0)

    if args[0] == "--delete":
        if len(args) < 2:
            print("Error: --delete requires a filename argument.")
            sys.exit(1)
        doc_id = args[1]
        collection = get_collection(CHROMA_PATH, OLLAMA_URL, EMBED_MODEL, COLLECTION)
        removed = delete_chunks(collection, doc_id)
        if removed:
            print(f"Deleted {removed} chunk(s) for '{doc_id}'.")
        else:
            print(f"No chunks found for '{doc_id}'.")
        sys.exit(0)

    target = Path(args[0])
    if not target.exists():
        print(f"Path not found: {target}")
        sys.exit(1)

    collection = get_collection(CHROMA_PATH, OLLAMA_URL, EMBED_MODEL, COLLECTION)

    files = (
        [target]
        if target.is_file()
        else [f for f in target.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS]
    )

    if not files:
        print("No supported files found.")
        sys.exit(0)

    for f in files:
        process_file(f, collection)

    print(f"\nDone. {len(files)} file(s) ingested.")


if __name__ == "__main__":
    main()
