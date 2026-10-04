"""The chunker. Pure functions, no DB and no model, so these are exact assertions
rather than shape checks -- chunking is the part of `embed` that silently degrades
retrieval if it drifts, and nothing downstream would fail loudly.
"""

from app.processing.chunk import CHUNK_CHARS, OVERLAP_CHARS, chunk_page, chunk_pages


def test_short_text_is_one_chunk():
    chunks = chunk_page(1, "a claim denial letter")
    assert len(chunks) == 1
    assert chunks[0].text == "a claim denial letter"
    assert (chunks[0].page_no, chunks[0].idx) == (1, 0)


def test_blank_and_whitespace_pages_produce_nothing():
    # A blank sheet still has a document_pages row (page_no alignment), but there is
    # nothing to embed and a zero vector is not the same as "no vector".
    assert chunk_page(2, "") == []
    assert chunk_page(2, "   \n\n  \t ") == []
    assert chunk_page(2, None) == []


def test_long_text_splits_and_overlaps():
    # Sentences the cutter can land on, well past one chunk's budget.
    text = " ".join(f"Sentence number {i} about the claim." for i in range(200))
    chunks = chunk_page(3, text)

    assert len(chunks) > 1
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert all(c.page_no == 3 for c in chunks)
    assert all(len(c.text) <= CHUNK_CHARS for c in chunks)
    # Consecutive chunks share a tail: the overlap exists, which is the whole reason
    # a sentence spanning a cut is still retrievable whole.
    assert chunks[1].text[:40] in chunks[0].text


def test_no_chunk_loses_text_from_the_middle():
    text = " ".join(f"Line {i} of the medical record." for i in range(300))
    joined = chunk_page(4, text)
    # Every chunk is a real substring of the page, and the last one reaches the end.
    assert all(c.text in text for c in joined)
    assert text.endswith(joined[-1].text)
    assert text.startswith(joined[0].text)


def test_unbroken_run_is_hard_cut_rather_than_looping():
    # No spaces, no sentence ends: a bad OCR smear or a base64 blob. The cutter has
    # no boundary to snap to and must still terminate.
    text = "x" * (CHUNK_CHARS * 3)
    chunks = chunk_page(5, text)

    assert len(chunks) == 4  # 3 full windows, advancing by CHUNK_CHARS - OVERLAP
    assert all(len(c.text) <= CHUNK_CHARS for c in chunks)
    assert chunks[0].text == "x" * CHUNK_CHARS


def test_chunks_never_span_two_pages():
    # Page provenance is what lets a RAG answer cite a sheet of paper, so a chunk
    # must never be built from the tail of one page and the head of the next.
    short = "the first page is short"
    chunks = chunk_pages([(1, short), (2, "the second page is also short")])

    assert [c.page_no for c in chunks] == [1, 2]
    assert chunks[0].text == short
    # idx restarts per page -- it is half of the vector store's point id, and the
    # other half is page_no.
    assert [c.idx for c in chunks] == [0, 0]


def test_blank_pages_do_not_break_numbering():
    chunks = chunk_pages([(1, "first"), (2, ""), (3, "third")])
    assert [(c.page_no, c.text) for c in chunks] == [(1, "first"), (3, "third")]


def test_overlap_is_smaller_than_the_window():
    # Guard the constants themselves: overlap >= window would never advance.
    assert 0 <= OVERLAP_CHARS < CHUNK_CHARS
