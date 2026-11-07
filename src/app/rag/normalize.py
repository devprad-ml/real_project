''' Record bytes -> derived searchable PDF/A.

The record is never touched. Everything here produces a *copy* written to a separate
derived blob key, so retention / legal hold on the original is unaffected.

ocrmypdf, img2pdf and pypdf are imported inside the functions that need them: they pull
Tesseract + Ghostscript, which are not installed on a bare Windows host. Keeping the
imports local means the app and its tests run without an OCR toolchain present.
'''

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import filetype

from app.errors import PermanentError

# img2pdf embeds these losslessly, no re-encode.
IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff"}

OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/rtf",
}

# A born-digital PDF carries a real text layer; a scan carries none. Below this many
# extracted characters over the sampled pages we assume it needs OCR.
TEXT_LAYER_MIN_CHARS = 100
TEXT_LAYER_SAMPLE_PAGES = 3


def sniff(data: bytes) -> str:
    ''' Classify by magic bytes, never by extension -- email attachments lie
    (a .pdf that is HTML, a .tiff that is JPEG). '''
    kind = filetype.guess(data)
    if kind is None:
        raise PermanentError("unsupported_type: no recognizable magic bytes")
    if kind.mime == "application/pdf":
        return "pdf"
    if kind.mime in IMAGE_MIMES:
        return "image"
    if kind.mime in OFFICE_MIMES:
        return "office"
    raise PermanentError(f"unsupported_type: {kind.mime}")


def to_pdf(kind: str, data: bytes) -> bytes:
    ''' Any accepted input -> a plain PDF. PDF/A conformance happens later. '''
    if kind == "pdf":
        return data
    if kind == "image":
        import img2pdf

        return img2pdf.convert(data)
    if kind == "office":
        return _libreoffice_to_pdf(data)
    raise PermanentError(f"unsupported_type: {kind}")


def _libreoffice_to_pdf(data: bytes) -> bytes:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        # Missing binary is an environment fault, not a bad document -- a plain
        # exception so it retries and the operator sees the backlog, rather than
        # quietly condemning every DOCX that arrives to FAILED.
        raise RuntimeError("LibreOffice (soffice) not on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(data)
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        out = Path(tmp) / "in.pdf"
        if not out.exists():
            raise PermanentError("office conversion produced no PDF")
        return out.read_bytes()


def has_text_layer(pdf: bytes) -> bool:
    ''' Born-digital PDFs already have selectable text -- OCRing them would rasterize
    perfectly good text and make it worse.

    ponytail: character count only. It cannot tell good text from a garbage embedded
    layer (NORMALIZATION.md open question 12.8.3); upgrade is a dictionary-hit ratio
    or a sample OCR comparison when bad born-digital PDFs actually show up.
    '''
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    if reader.is_encrypted:
        raise PermanentError("encrypted PDF")
    chars = sum(
        len(page.extract_text() or "")
        for page in reader.pages[:TEXT_LAYER_SAMPLE_PAGES]
    )
    return chars >= TEXT_LAYER_MIN_CHARS


def page_count(pdf: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


def to_pdfa(pdf: bytes) -> bytes:
    ''' PDF/A-2b with a text layer, via ocrmypdf. Scans only -- a born-digital PDF
    already has a text layer and never reaches this call (see run()); OCRing it would
    rasterize perfectly good text and, for a fillable form, strip the AcroForm that
    carries the actual field values. '''
    import ocrmypdf

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        dst = Path(tmp) / "out.pdf"
        src.write_bytes(pdf)
        opts = {
            "output_type": "pdfa", "progress_bar": False,
            "deskew": True, "rotate_pages": True,
            # The default of 14 left a page scanned sideways unrotated, which came back
            # with no text at all.
            "rotate_pages_threshold": 2.0,
            # A short letter falls under the text-layer threshold but has text, and a
            # mixed PDF has both kinds of page: OCR only the pages that need it.
            "skip_text": True,
            # Despeckle is a nicety: without unpaper, ocrmypdf would refuse the whole
            # document if this were set, and OCR works fine without it.
            "clean": shutil.which("unpaper") is not None,
        }
        ocrmypdf.ocr(src, dst, **opts)
        return dst.read_bytes()


def run(data: bytes) -> tuple[bytes, int]:
    ''' The whole normalize step: record bytes -> (normalized PDF bytes, page count).

    A born-digital PDF (real text layer, or a fillable form with field values) is
    returned as-is: OCR would gain nothing and, for a fillable form, destroys the
    AcroForm that pdftext reads field values from. Only a scan goes through OCR. '''
    from pypdf.errors import PdfReadError

    pdf = to_pdf(sniff(data), data)
    try:
        if has_text_layer(pdf):
            return pdf, page_count(pdf)
    except PdfReadError as exc:
        # Damaged bytes stay damaged, so retrying only delays telling anyone.
        raise PermanentError(f"unreadable PDF: {type(exc).__name__}") from exc
    pdfa = to_pdfa(pdf)
    return pdfa, page_count(pdfa)
