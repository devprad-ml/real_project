''' Derived PDF/A -> per-page text.

By the time this runs, `normalize` has already put a text layer in the PDF (Tesseract
for scans, the original for born-digital). So this step is extraction, not recognition
-- it just pulls the layer back out, one row per page.

Per-page rather than one blob: page-level retrieval, page citations for later
extraction, and the ability to re-OCR a single bad page.
'''

import io
from typing import Iterator


def pages(pdf: bytes) -> Iterator[tuple[int, str]]:
    ''' Yield (page_no, text), 1-indexed. A page with no text yields "" rather than
    being skipped -- page_no must stay aligned with the physical document. '''
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    for i, page in enumerate(reader.pages, start=1):
        yield i, page.extract_text() or ""
