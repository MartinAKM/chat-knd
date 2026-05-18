import functools

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# Module-level caches — models and clients are loaded once per process.
_clients: dict[str, chromadb.PersistentClient] = {}
_collections: dict[tuple, object] = {}


@functools.lru_cache(maxsize=4)
def _get_ef(model_name: str) -> SentenceTransformerEmbeddingFunction:
    return SentenceTransformerEmbeddingFunction(model_name=model_name)


def _get_client(chroma_path: str) -> chromadb.PersistentClient:
    if chroma_path not in _clients:
        _clients[chroma_path] = chromadb.PersistentClient(path=chroma_path)
    return _clients[chroma_path]


def get_collection(chroma_path: str, embed_model: str, collection_name: str):
    key = (chroma_path, embed_model, collection_name)
    if key not in _collections:
        ef = _get_ef(embed_model)
        _collections[key] = _get_client(chroma_path).get_or_create_collection(
            name=collection_name, embedding_function=ef
        )
    return _collections[key]


def invalidate_collection_cache(chroma_path: str, embed_model: str, collection_name: str) -> None:
    """Drop the cached collection reference (call before dropping/recreating a collection)."""
    _collections.pop((chroma_path, embed_model, collection_name), None)


def upsert_chunks(collection, doc_id: str, chunks: list[str]) -> None:
    ids = [f"{doc_id}__{i}" for i in range(len(chunks))]
    metadatas = [{"source": doc_id}] * len(chunks)
    collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)


def delete_chunks(collection, doc_id: str) -> int:
    """Delete all chunks belonging to doc_id. Returns the number of chunks removed."""
    existing = collection.get(where={"source": doc_id}, include=[])
    count = len(existing["ids"])
    if count:
        collection.delete(where={"source": doc_id})
    return count
