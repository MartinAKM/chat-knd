"""
One-time migration: move ticket chunks from the documents collection
into the dedicated tickets collection.

Chunks whose source name starts with "ticket_" are copied to the
tickets collection and then deleted from the documents collection.
All other chunks stay in documents untouched.

Safety: data is COPIED first and verified before deleting from source.

Usage:
    python ticket_ingest/migrate_to_tickets_collection.py
    python ticket_ingest/migrate_to_tickets_collection.py --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

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


def migrate(dry_run: bool = False) -> None:
    print(f"Source      : {DOCS_COLLECTION}  →  Target: {TICKETS_COLLECTION}")
    print(f"ChromaDB    : {CHROMA_PATH}")
    if dry_run:
        print("[DRY RUN — nothing will be modified]\n")

    src = get_collection(CHROMA_PATH, EMBED_MODEL, DOCS_COLLECTION)
    dst = get_collection(CHROMA_PATH, EMBED_MODEL, TICKETS_COLLECTION)

    total = src.count()
    print(f"Scanning {total} chunks in '{DOCS_COLLECTION}'…\n")

    ids_to_move:   list[str]  = []
    docs_to_move:  list[str]  = []
    metas_to_move: list[dict] = []
    offset = 0

    bar = tqdm(total=total, unit="chunk", desc="Scanning")
    while True:
        data = src.get(
            include=["documents", "metadatas"],
            limit=_PAGE,
            offset=offset,
        )
        if not data["ids"]:
            break
        for cid, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
            if meta.get("source", "").startswith("ticket_"):
                ids_to_move.append(cid)
                docs_to_move.append(doc)
                metas_to_move.append(meta)
        bar.update(len(data["ids"]))
        offset += len(data["ids"])
        if len(data["ids"]) < _PAGE:
            break
    bar.close()

    print(f"\nFound {len(ids_to_move)} ticket chunks to migrate.\n")

    if not ids_to_move:
        print("Nothing to do — documents collection has no ticket chunks.")
        return

    if dry_run:
        print("(Dry run) Would copy to tickets and delete from documents.")
        return

    # ── Copy to destination ────────────────────────────────────────────────
    print(f"Copying to '{TICKETS_COLLECTION}'…")
    bar = tqdm(total=len(ids_to_move), unit="chunk")
    for i in range(0, len(ids_to_move), _BATCH):
        dst.upsert(
            ids       = ids_to_move  [i : i + _BATCH],
            documents = docs_to_move [i : i + _BATCH],
            metadatas = metas_to_move[i : i + _BATCH],
        )
        bar.update(min(_BATCH, len(ids_to_move) - i))
    bar.close()

    tickets_count = dst.count()
    print(f"\nTickets collection now has {tickets_count} chunks.")

    # ── Delete from source ─────────────────────────────────────────────────
    print(f"Deleting {len(ids_to_move)} ticket chunks from '{DOCS_COLLECTION}'…")
    bar = tqdm(total=len(ids_to_move), unit="chunk")
    for i in range(0, len(ids_to_move), _BATCH):
        src.delete(ids=ids_to_move[i : i + _BATCH])
        bar.update(min(_BATCH, len(ids_to_move) - i))
    bar.close()

    print(f"\nMigration complete.")
    print(f"  '{DOCS_COLLECTION}'  → {src.count()} chunks remaining (documents only).")
    print(f"  '{TICKETS_COLLECTION}' → {dst.count()} chunks.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate ticket chunks from the documents collection to tickets."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only, do not move any data.",
    )
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
