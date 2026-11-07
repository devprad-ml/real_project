''' Normalized PDF -> per-page text.

By the time this runs, `normalize` has already put a text layer in the PDF (Tesseract
for scans, the original for born-digital). So this is extraction, not recognition: it
pulls the layer back out. The normalized blob is the only stored copy of the text --
classify and embed both read it from here, and a rebuilt vector collection re-reads it
the same way.
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
        yield i, _page_text(page)


def _page_text(page) -> str:
    ''' A fillable form's own field values, not the page's raw content-stream text.

    extract_text() walks glyphs in draw order, which for a form is widget-creation
    order, not visual order -- it dumps every filled field as one scrambled block
    disconnected from its label. The AcroForm itself has no such ordering problem: each
    widget already names its own field, so fields go first. The printed text still follows,
    because that is where the form's title and instructions are. '''
    fields = "\n".join(f"{k}: {v}" for k, v in _page_fields(page).items())
    return "\n\n".join(part for part in (fields, page.extract_text() or "") if part)


def _page_fields(page) -> dict[str, str]:
    fields: dict[str, str] = {}
    for annot in page.get("/Annots") or []:
        widget = annot.get_object()
        if widget.get("/Subtype") != "/Widget":
            continue
        parent = widget.get("/Parent")
        parent = parent.get_object() if parent is not None else {}
        name = widget.get("/T") or parent.get("/T")
        value = widget.get("/V") if "/V" in widget else parent.get("/V")
        if not name or value in (None, ""):
            continue
        fields[str(name)] = str(value)
    return fields
