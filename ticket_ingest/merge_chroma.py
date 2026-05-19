"""
Merge ChromaDB collections from other machines into the local master collection.

Workflow:
  1. On each remote PC, finish the ingestion run.
  2. Copy the remote chroma_data folder to this machine, e.g.:
         chroma_data_pc2/   chroma_data_pc3/   chroma_data_pc4/
  3. Run this script pointing at those folders:
         python ticket_ingest/merge_chroma.py chroma_data_pc2 chroma_data_pc3 chroma_data_pc4

Only chunks whose source does not already exist in the local collection are imported.
Chunks that already exist are skipped — safe to run multiple times.
"""

import argparse
import os
import sys
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from tqdm import tqdm

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "doc_reader"))

from chroma_store import get_collection  # noqa: E402

CHROMA_PATH     = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL     = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "documents")

_PAGE  = 500   # chunks per read page
_BATCH = 200   # chunks per upsert call


def _load_known_sources(col) -> set[str]:
    """Return the set of all source names already in the collection."""
    known: set[str] = set()
    offset = 0
    while True:
        data = col.get(include=["metadatas"], limit=1000, offset=offset)
        if not data["ids"]:
            break
        for meta in data["metadatas"]:
            known.add(meta.get("source", ""))
        offset += len(data["ids"])
        if len(data["ids"]) < 1000:
            break
    return known


def merge(source_paths: list[str]) -> None:
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    ef = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)

    dst = get_collection(CHROMA_PATH, EMBED_MODEL, COLLECTION_NAME)

    print("Loading existing sources from destination…", end=" ", flush=True)
    known_sources = _load_known_sources(dst)
    print(f"{len(known_sources)} sources found.\n")

    total_imported = 0
    total_skipped  = 0

    for src_path in source_paths:
        src_path = str(src_path)
        print(f"── Source: {src_path}")

        try:
            src_client  = chromadb.PersistentClient(path=src_path)
            collections = src_client.list_collections()
            if not collections:
                # Check if chroma.sqlite3 exists one level deeper (common unzip artifact)
                import glob as _glob
                nested = _glob.glob(f"{src_path}/*/chroma.sqlite3")
                hint   = f" Did you mean: {nested[0].replace('chroma.sqlite3','').rstrip('/\\')}?" if nested else ""
                print(f"   No collections found in this folder — check the path.{hint}\n")
                continue
            col_names = [c.name if hasattr(c, "name") else str(c) for c in collections]
            src_name  = COLLECTION_NAME if COLLECTION_NAME in col_names else col_names[0]
            if src_name != COLLECTION_NAME:
                print(f"   Note: collection '{COLLECTION_NAME}' not found; using '{src_name}' instead.")
            src_col = src_client.get_collection(name=src_name, embedding_function=ef)
        except Exception as e:
            print(f"   ERROR opening collection: {e}\n")
            continue

        total_chunks = src_col.count()
        if not total_chunks:
            print("   Empty collection — nothing to merge.\n")
            continue

        print(f"   {total_chunks} chunks to scan…")

        # Collect new chunks from this source
        new_ids:   list[str]  = []
        new_docs:  list[str]  = []
        new_metas: list[dict] = []
        imported = 0
        skipped  = 0

        bar    = tqdm(total=total_chunks, unit="chunk", dynamic_ncols=True)
        offset = 0

        while True:
            data = src_col.get(
                include=["documents", "metadatas"],
                limit=_PAGE,
                offset=offset,
            )
            if not data["ids"]:
                break

            for chunk_id, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
                source = meta.get("source", "")
                if source in known_sources:
                    skipped += 1
                else:
                    new_ids.append(chunk_id)
                    new_docs.append(doc)
                    new_metas.append(meta)
                    known_sources.add(source)
                    imported += 1

            bar.update(len(data["ids"]))
            offset += len(data["ids"])
            if len(data["ids"]) < _PAGE:
                break

        bar.close()

        # Upsert new chunks in batches so the embedding model works efficiently
        if new_ids:
            print(f"   Upserting {imported} new chunks…", end=" ", flush=True)
            for i in range(0, len(new_ids), _BATCH):
                dst.upsert(
                    ids       = new_ids  [i : i + _BATCH],
                    documents = new_docs [i : i + _BATCH],
                    metadatas = new_metas[i : i + _BATCH],
                )
            print("done.")

        print(f"   Imported: {imported}  |  Already existed: {skipped}\n")
        total_imported += imported
        total_skipped  += skipped

    print(f"Merge complete.  Total imported: {total_imported}  |  Total skipped: {total_skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Merge ChromaDB collections from remote machines into the local master."
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="CHROMA_PATH",
        help="Path(s) to the remote chroma_data folders copied to this machine.",
    )
    args = parser.parse_args()
    merge(args.sources)
