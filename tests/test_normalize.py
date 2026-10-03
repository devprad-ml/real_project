"""Type sniffing. Pure functions, no DB, no OCR toolchain.

The conversion and PDF/A steps are not covered here -- they need Tesseract and
Ghostscript on PATH, which is a container concern, not a unit-test one.
"""

import pytest

from app.errors import PermanentError
from app.processing.normalize import sniff

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
