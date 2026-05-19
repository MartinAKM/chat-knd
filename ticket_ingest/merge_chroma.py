"""
Merge ChromaDB data from remote machines into the local two-collection store.

Chunks are routed by source name:
  source starts with "ticket_"  →  tickets collection
  anything else                 →  documents collection

Works whether the source machine used one collection (old) or two (new).
Safe to run multiple times — already-present sources are skipped.

Workflow:
  1. On each remote machine, finish the ingestion run.
  2. Copy the remote chroma_data folder to this machine, e.g.:
         chroma_data_pc2/   chroma_data_pc3/
  3. Run:
         python ticket_ingest/merge_chroma.py chroma_data_pc2 chroma_data_pc3
"""

import argparse
import glob as _glob
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

CHROMA_PATH        = os.getenv("CHROMA_PATH", "chroma_data")
EMBED_MODEL        = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
DOCS_COLLECTION    = os.getenv("CHROMA_COLLECTION", "documents")
TICKETS_COLLECTION = os.getenv("TICKETS_CHROMA_COLLECTION", "tickets")

_PAGE  = 500
_BATCH = 200


def _load_known_sources(col) -> set[str]:
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

    dst_docs    = get_collection(CHROMA_PATH, EMBED_MODEL, DOCS_COLLECTION)
    dst_tickets = get_collection(CHROMA_PATH, EMBED_MODEL, TICKETS_COLLECTION)

    print("Loading known sources from destination…", end=" ", flush=True)
    known_docs    = _load_known_sources(dst_docs)
    known_tickets = _load_known_sources(dst_tickets)
    print(f"{len(known_docs)} doc sources, {len(known_tickets)} ticket sources.\n")

    grand_docs    = 0
    grand_tickets = 0
    grand_skipped = 0

    for src_path in source_paths:
        src_path = str(src_path)
        print(f"── Source: {src_path}")

        try:
            src_client  = chromadb.PersistentClient(path=src_path)
            collections = src_client.list_collections()
        except Exception as e:
            print(f"   ERROR opening source: {e}\n")
            continue

        if not collections:
            nested = _glob.glob(f"{src_path}/*/chroma.sqlite3")
            hint   = (
                f" Did you mean: {nested[0].replace('chroma.sqlite3', '').rstrip('/\\')}?"
                if nested else ""
            )
            print(f"   No collections found — check the path.{hint}\n")
            continue

        col_names = [c.name if hasattr(c, "name") else str(c) for c in collections]
        print(f"   Collections found: {', '.join(col_names)}")

        # Collect new chunks from ALL collections in this source, routing by source name.
        # Key: chunk_id  Value: (text, meta)
        new_docs:    dict[str, tuple[str, dict]] = {}
        new_tickets: dict[str, tuple[str, dict]] = {}
        src_skipped = 0

        for col_name in col_names:
            try:
                src_col = src_client.get_collection(name=col_name, embedding_function=ef)
            except Exception as e:
                print(f"   ERROR opening '{col_name}': {e}")
                continue

            total_chunks = src_col.count()
            if not total_chunks:
                print(f"   '{col_name}': empty, skipping.")
                continue

            print(f"   '{col_name}': {total_chunks} chunks…")
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

                for cid, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
                    source = meta.get("source", "")
                    if source.startswith("ticket_"):
                        if source in known_tickets:
                            src_skipped += 1
                        elif cid not in new_tickets:
                            new_tickets[cid] = (doc, meta)
                    else:
                        if source in known_docs:
                            src_skipped += 1
                        elif cid not in new_docs:
                            new_docs[cid] = (doc, meta)

                bar.update(len(data["ids"]))
                offset += len(data["ids"])
                if len(data["ids"]) < _PAGE:
                    break

            bar.close()

        # ── Upsert document chunks ─────────────────────────────────────────
        if new_docs:
            ids   = list(new_docs)
            docs  = [new_docs[i][0] for i in ids]
            metas = [new_docs[i][1] for i in ids]
            print(f"   Upserting {len(ids)} document chunks…", end=" ", flush=True)
            for i in range(0, len(ids), _BATCH):
                dst_docs.upsert(
                    ids       = ids  [i : i + _BATCH],
                    documents = docs [i : i + _BATCH],
                    metadatas = metas[i : i + _BATCH],
                )
            print("done.")
            for meta in metas:
                known_docs.add(meta.get("source", ""))

        # ── Upsert ticket chunks ───────────────────────────────────────────
        if new_tickets:
            ids   = list(new_tickets)
            docs  = [new_tickets[i][0] for i in ids]
            metas = [new_tickets[i][1] for i in ids]
            print(f"   Upserting {len(ids)} ticket chunks…", end=" ", flush=True)
            for i in range(0, len(ids), _BATCH):
                dst_tickets.upsert(
                    ids       = ids  [i : i + _BATCH],
                    documents = docs [i : i + _BATCH],
                    metadatas = metas[i : i + _BATCH],
                )
            print("done.")
            for meta in metas:
                known_tickets.add(meta.get("source", ""))

        print(
            f"   Documents: {len(new_docs)} new  |  "
            f"Tickets: {len(new_tickets)} new  |  "
            f"Skipped: {src_skipped}\n"
        )
        grand_docs    += len(new_docs)
        grand_tickets += len(new_tickets)
        grand_skipped += src_skipped

    print(
        f"Merge complete.\n"
        f"  Documents imported : {grand_docs}\n"
        f"  Tickets imported   : {grand_tickets}\n"
        f"  Total skipped      : {grand_skipped}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Merge ChromaDB data from remote machines into the local store."
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="CHROMA_PATH",
        help="Path(s) to the remote chroma_data folders copied to this machine.",
    )
    args = parser.parse_args()
    merge(args.sources)
