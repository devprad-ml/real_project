''' Text -> BM25 sparse vector, for the keyword leg of hybrid search.

Dense embeddings blur exact tokens: a member ID, a CPT code or a claim number lands
near every other number. BM25 scores the literal token. The work is split the same way
fastembed's `Qdrant/bm25` splits it: this module computes the term-frequency half on
the client, and the collection's `Modifier.IDF` supplies IDF on the server, where the
corpus statistics live.

Pure functions, no I/O, no dependency.
'''

import re
import zlib
from collections import Counter

K1 = 1.2  # how fast repeated terms stop adding score
B = 0.75  # how much a long chunk is penalised for its length
# ponytail: fixed average chunk length (~1000 chars of prose), since qdrant does not
# track avgdl. Close enough while CHUNK_CHARS is fixed; recompute if chunking changes.
AVG_LEN = 160

# Splits on anything that isn't a letter or digit: "MBR-0048213" -> "mbr", "0048213",
# and a query for the same ID tokenises the same way.
# ponytail: no stemming or stopwords, so "denied" does not match "denial". Dense covers
# paraphrase; add fastembed's bm25 if word-form recall turns out to matter.
_TOKEN = re.compile(r"[a-z0-9]+")

Sparse = tuple[list[int], list[float]]


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _index(token: str) -> int:
    # crc32, not hash(): Python salts str hashes per process, and these indices must
    # match between the worker that wrote a chunk and the API that queries it.
    return zlib.crc32(token.encode())


def encode_doc(text: str) -> Sparse:
    toks = tokens(text)
    if not toks:
        return [], []
    tf = Counter(_index(t) for t in toks)
    norm = K1 * (1 - B + B * len(toks) / AVG_LEN)
    indices = sorted(tf)
    return indices, [tf[i] * (K1 + 1) / (tf[i] + norm) for i in indices]


def encode_query(text: str) -> Sparse:
    ''' One per distinct term: the query side of BM25 carries no tf weighting. '''
    indices = sorted({_index(t) for t in tokens(text)})
    return indices, [1.0] * len(indices)
