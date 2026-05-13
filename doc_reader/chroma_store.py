import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction


def get_collection(chroma_path: str, embed_model: str, collection_name: str):
    client = chromadb.PersistentClient(path=chroma_path)
    ef = SentenceTransformerEmbeddingFunction(model_name=embed_model)
    return client.get_or_create_collection(name=collection_name, embedding_function=ef)


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
