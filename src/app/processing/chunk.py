''' OCR text -> overlapping chunks, one embedding each.

Pure functions, no model and no I/O: chunking is the one part of `embed` worth
testing exactly, and it stays testable only while nothing here touches a GPU or a
socket. Chunks never span a page -- every chunk keeps the `page_no` it came from so a
RAG answer can cite a real sheet of paper.
'''

from dataclasses import dataclass
from typing import Iterable

# ponytail: character budget, not tokens. ~1000 chars averages ~250 tokens, under
# all-MiniLM-L6-v2's 256-token window; text that blows the window is truncated by the
# model, not by us. Add `tiktoken` (or the model's own tokenizer) if truncation shows
# up in retrieval quality.
CHUNK_CHARS = 1000
OVERLAP_CHARS = 150

# A cut is only allowed in the last 40% of the window. Without a floor, a page of
# one-line sentences would end every chunk at the first newline it found.
_MIN_FILL = 0.6

# Tried in order. First one that lands past the floor wins.
_BOUNDARIES = ("\n\n", ". ", ".\n", "\n", " ")


@dataclass(frozen=True)
class Chunk:
    page_no: int
    idx: int  # 0-based, per page -- half of the vector store's point id
    text: str


def _cut(text: str, start: int) -> int:
    ''' Index to end a chunk starting at `start`, snapped back to a boundary. '''
    end = start + CHUNK_CHARS
    if end >= len(text):
        return len(text)

    window = text[start:end]
    floor = int(CHUNK_CHARS * _MIN_FILL)
    for sep in _BOUNDARIES:
        i = window.rfind(sep)
        if i >= floor:
            return start + i + len(sep)
    # Unbroken run of 600+ chars (a table, a base64 blob, a bad OCR smear). Hard cut
    # mid-word beats looping or emitting the whole page as one chunk.
    return end


def chunk_page(page_no: int, text: str) -> list[Chunk]:
    text = (text or "").strip()
    if not text:
        return []  # blank sheet: nothing to embed, and a zero vector is not "empty"

    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = _cut(text, start)
        piece = text[start:end].strip()
        if piece:
            chunks.append(Chunk(page_no=page_no, idx=len(chunks), text=piece))
        if end >= len(text):
            break
        # Overlap so a sentence split across the cut is still retrievable whole from
        # one side of it. The restart is not boundary-snapped -- overlap is a recall
        # hedge, and a chunk that opens mid-word costs nothing at the vector level.
        start = max(end - OVERLAP_CHARS, start + 1)
    return chunks


def chunk_pages(pages: Iterable[tuple[int, str]]) -> list[Chunk]:
    ''' `pages` is (page_no, ocr_text) in page order. Takes tuples, not ORM rows, so
    re-chunking for a model upgrade can read from anywhere. '''
    return [c for page_no, text in pages for c in chunk_page(page_no, text)]
