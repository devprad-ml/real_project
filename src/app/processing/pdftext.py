''' Normalized PDF/A -> per-page text.

By the time this runs, `normalize` has already put a text layer in the PDF (Tesseract
for scans, the original for born-digital). So this is extraction, not recognition: it
pulls the layer back out. The PDF/A is the only stored copy of the text -- classify and
embed both read it from here, and a rebuilt vector collection re-reads it the same way.
'''

import io
from typing import Iterator


def pages(pdf: bytes) -> Iterator[tuple[int, str]]:
    ''' Yield (page_no, text), 1-indexed. A page with no text yields "" rather than
    being skipped -- page_no must stay aligned with the physical document. Lazy, so a
    caller that only needs page one never parses page five hundred. '''
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    for i, page in enumerate(reader.pages, start=1):
        yield i, page.extract_text() or ""
