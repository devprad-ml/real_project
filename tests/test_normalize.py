"""Type sniffing. Pure functions, no DB, no OCR toolchain.

The conversion and PDF/A steps are not covered here -- they need Tesseract and
Ghostscript on PATH, which is a container concern, not a unit-test one.
"""

import pytest

from app.errors import PermanentError
from app.rag import normalize
from app.rag.normalize import sniff, to_pdfa

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32


@pytest.mark.parametrize(
    "data,expected", [(PDF, "pdf"), (PNG, "image"), (JPEG, "image")]
)
def test_sniff_reads_magic_bytes(data, expected):
    assert sniff(data) == expected


def test_sniff_ignores_a_lying_extension():
    """An attachment named invoice.pdf that is actually a PNG routes by its bytes.
    Trusting the extension here is how a converter gets handed garbage."""
    assert sniff(PNG) == "image"


def test_sniff_rejects_unknown_content():
    with pytest.raises(PermanentError, match="unsupported_type"):
        sniff(b"just some plain text, no signature")


def _ocr_options(monkeypatch, tmp_path):
    ocrmypdf = pytest.importorskip("ocrmypdf")
    seen = {}

    def fake_ocr(src, dst, **opts):
        seen.update(opts)
        dst.write_bytes(b"%PDF-1.7 ocr output")

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    assert to_pdfa(b"%PDF-1.7 scan") == b"%PDF-1.7 ocr output"
    return seen


def test_ocr_keeps_pages_that_already_have_text(monkeypatch, tmp_path):
    """Without skip_text, a short letter under the text-layer threshold fails outright
    with PriorOcrFoundError."""
    assert _ocr_options(monkeypatch, tmp_path)["skip_text"] is True


def test_ocr_rotates_pages_it_is_only_fairly_sure_about(monkeypatch, tmp_path):
    """At the default threshold a sideways scan stays sideways and yields no text."""
    assert _ocr_options(monkeypatch, tmp_path)["rotate_pages_threshold"] == 2.0


@pytest.mark.parametrize(("found", "clean"), [(None, False), ("/bin/unpaper", True)])
def test_ocr_only_despeckles_when_unpaper_is_installed(
    monkeypatch, tmp_path, found, clean
):
    """clean=True without unpaper fails every scan with MissingDependencyError."""
    monkeypatch.setattr(normalize.shutil, "which", lambda name: found)
    assert _ocr_options(monkeypatch, tmp_path)["clean"] is clean


def test_a_damaged_pdf_is_a_permanent_failure():
    pytest.importorskip("pypdf")
    with pytest.raises(PermanentError, match="unreadable PDF"):
        normalize.run(b"%PDF-1.4\n1 0 obj\n<< /Type /Cat")
