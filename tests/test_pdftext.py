"""Page text extraction."""

from app.rag.pdftext import _page_text


class _Page(dict):
    def __init__(self, text, annots=()):
        super().__init__({"/Annots": list(annots)})
        self._text = text

    def extract_text(self):
        return self._text


class _Widget(dict):
    def get_object(self):
        return self


def test_a_filled_form_keeps_its_printed_text_as_well_as_its_fields():
    widget = _Widget({"/Subtype": "/Widget", "/T": "member_id", "/V": "M-1001"})

    text = _page_text(_Page("PRIOR AUTHORIZATION REQUEST", [widget]))

    assert "member_id: M-1001" in text
    assert "PRIOR AUTHORIZATION REQUEST" in text


def test_a_page_with_no_form_is_just_its_text():
    assert _page_text(_Page("plain text")) == "plain text"


def test_a_blank_page_is_empty():
    assert _page_text(_Page("")) == ""
