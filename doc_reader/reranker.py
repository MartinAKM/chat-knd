import functools

from sentence_transformers import CrossEncoder

_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


@functools.lru_cache(maxsize=1)
def _get_model() -> CrossEncoder:
    return CrossEncoder(_MODEL)


def rerank(query: str, chunks: list[str]) -> list[int]:
    """Return chunk indices sorted by cross-encoder relevance score (best first)."""
    if not chunks:
        return []
    scores = _get_model().predict([(query, c) for c in chunks])
    return sorted(range(len(chunks)), key=lambda i: float(scores[i]), reverse=True)
