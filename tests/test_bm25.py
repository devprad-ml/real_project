"""BM25 sparse vectors. Pure functions, no store."""

import zlib

from app.processing.bm25 import encode_doc, encode_query, tokens


def test_ids_and_codes_tokenise_the_same_on_both_sides():
    """A member ID typed into search must produce the indices the chunk was stored
    with, or the keyword leg can never match it."""
    assert tokens("Member MBR-0048213, CPT 99213.") == [
        "member", "mbr", "0048213", "cpt", "99213",
    ]
    stored, _ = encode_doc("appeal for MBR-0048213")
    queried, _ = encode_query("mbr-0048213")
    assert set(queried) <= set(stored)


def test_indices_are_stable_across_processes():
    """crc32, not hash(): the worker that wrote a chunk and the API that queries it are
    different processes, and str hashes are salted per process."""
    indices, _ = encode_query("denial")
    assert indices == [zlib.crc32(b"denial")]


def test_repeated_terms_saturate():
    """tf 10 must score more than tf 1, but far less than 10x: that is what k1 is for."""
    one = dict(zip(*encode_doc("denial " + "filler " * 9)))
    ten = dict(zip(*encode_doc("denial " * 10)))
    key = zlib.crc32(b"denial")
    assert one[key] < ten[key] < 10 * one[key]


def test_no_terms_means_an_empty_vector():
    assert encode_doc("-- . --") == ([], [])
    assert encode_query("") == ([], [])


def test_query_weights_are_one_per_distinct_term():
    indices, values = encode_query("denial denial appeal")
    assert len(indices) == 2
    assert values == [1.0, 1.0]
