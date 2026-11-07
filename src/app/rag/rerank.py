''' Scores (query, chunk) pairs with a cross-encoder, which reads the two together and
so ranks far better than comparing separate embeddings. Too slow to run over a whole
collection, so it only reorders what hybrid search already found. '''

from functools import lru_cache

from app.config import get_settings


@lru_cache
def _model():
    from sentence_transformers import CrossEncoder

    return CrossEncoder(get_settings().rerank_model)


def score(query: str, texts: list[str]) -> list[float]:
    ''' One score per text, higher is better. Unbounded, and only comparable within one
    model. '''
    if not texts:
        return []
    return _model().predict([(query, text) for text in texts]).tolist()
