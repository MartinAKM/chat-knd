"""
BM25 sparse retrieval, one in-memory index per ChromaDB collection.

Indexes are built lazily on the first search call and live for the
duration of the server process.  Call invalidate() after ingesting new
data so the index is rebuilt on the next query.

Public API
----------
ensure_built(collection_name, chroma_path, embed_model)
search(query, collection_name, n)  → [(id, text, meta), ...]
rrf_fuse(dense, sparse, k)         → [(id, text, meta, rrf_score), ...]
invalidate(collection_name)
"""

import re

from rank_bm25 import BM25Okapi

# Minimal Portuguese stopword list — keeps domain terms like "produto",
# "nota", "fiscal", "cadastrar" fully intact.
_PT_STOPWORDS = frozenset({
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos",
    "e", "em", "é", "essa", "esse", "este", "esta", "isso", "na",
    "nas", "no", "nos", "o", "os", "ou", "para", "que", "se", "ter",
    "tem", "tinha", "teve", "há", "uma", "um", "por", "foi", "ser",
    "não", "mais", "mas", "como", "também", "já", "ainda", "quando",
    "onde", "porque", "pois", "então", "assim", "ele", "ela", "eles",
    "elas", "seu", "sua", "seus", "suas", "me", "nos", "lhe", "lhes",
})

# collection_name → (BM25Okapi | None, [(chunk_id, text, meta)])
_indexes: dict[str, tuple] = {}


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric boundaries, remove stopwords."""
    tokens = re.findall(r"[A-Za-z0-9À-ÿ\-]+", text.lower())
    return [t for t in tokens if t not in _PT_STOPWORDS and len(t) > 1]


def build(collection_name: str, chroma_path: str, embed_model: str) -> None:
    """Read every chunk from a ChromaDB collection and build a BM25 index."""
    from chroma_store import get_collection  # noqa: PLC0415

    col   = get_collection(chroma_path, embed_model, collection_name)
    total = col.count()
    if total == 0:
        _indexes[collection_name] = (None, [])
        return

    corpus: list[tuple[str, str, dict]] = []
    offset = 0
    while True:
        data = col.get(include=["documents", "metadatas"], limit=500, offset=offset)
        if not data["ids"]:
            break
        for chunk_id, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
            corpus.append((chunk_id, doc or "", meta))
        offset += len(data["ids"])
        if len(data["ids"]) < 500:
            break

    tokenized = [_tokenize(doc) for _, doc, _ in corpus]
    _indexes[collection_name] = (BM25Okapi(tokenized), corpus)


def ensure_built(collection_name: str, chroma_path: str, embed_model: str) -> None:
    """Build the index on first call; no-op if already built."""
    if collection_name not in _indexes:
        build(collection_name, chroma_path, embed_model)


def search(
    query: str,
    collection_name: str,
    n: int,
) -> list[tuple[str, str, dict]]:
    """
    Return up to n chunks ranked by BM25 score (best first).
    Only chunks with a positive score are included.
    Returns list of (chunk_id, text, meta).
    """
    if collection_name not in _indexes:
        return []
    bm25, corpus = _indexes[collection_name]
    if bm25 is None or not corpus:
        return []
    tokens = _tokenize(query)
    if not tokens:
        return []
    scores = bm25.get_scores(tokens)
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [
        (corpus[i][0], corpus[i][1], corpus[i][2])
        for i in ranked[:n]
        if scores[i] > 0
    ]


def rrf_fuse(
    dense: list[tuple[str, str, dict, float]],  # (id, text, meta, distance)
    sparse: list[tuple[str, str, dict]],         # (id, text, meta)
    k: int = 60,
) -> list[tuple[str, str, dict, float]]:
    """
    Combine dense and BM25 results with Reciprocal Rank Fusion.
    Returns (id, text, meta, rrf_score) sorted best-first.
    """
    dense_rank  = {item[0]: i for i, item in enumerate(dense)}
    sparse_rank = {item[0]: i for i, item in enumerate(sparse)}

    all_chunks: dict[str, tuple[str, dict]] = {}
    for item in dense:
        all_chunks[item[0]] = (item[1], item[2])
    for item in sparse:
        all_chunks.setdefault(item[0], (item[1], item[2]))

    n_d, n_s = len(dense), len(sparse)
    fused_scores = {
        doc_id: 1.0 / (k + dense_rank.get(doc_id, n_d))
                + 1.0 / (k + sparse_rank.get(doc_id, n_s))
        for doc_id in all_chunks
    }

    return sorted(
        [
            (doc_id, all_chunks[doc_id][0], all_chunks[doc_id][1], score)
            for doc_id, score in fused_scores.items()
        ],
        key=lambda x: x[3],
        reverse=True,
    )


def invalidate(collection_name: str) -> None:
    """Drop the cached index so it is rebuilt on the next query."""
    _indexes.pop(collection_name, None)
