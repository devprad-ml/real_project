''' Chunk text -> vectors, with a local model. No API key, no per-call cost.

The only module that imports `sentence_transformers`, and it imports it inside the
function on purpose: the import pulls torch (hundreds of MB, seconds of load time),
and nothing but a real embed job should pay for that. Tests monkeypatch `encode`.
'''

from functools import lru_cache

from app.config import get_settings

# One batch call per handler run. Larger batches are faster per chunk but hold more
# in RAM; a 40-page fax is a few hundred chunks, so this never needs tuning locally.
BATCH_SIZE = 32


@lru_cache
def _model():
    ''' Loaded once per worker process, on first use. Weights download once, then
    come from the HF cache -- bake that cache into the worker image so a cold
    container does not pull 90MB before its first job. '''
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(get_settings().embed_model)


def encode(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return _model().encode(texts, batch_size=BATCH_SIZE).tolist()
