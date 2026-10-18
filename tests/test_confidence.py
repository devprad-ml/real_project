"""Confidence scoring and the review gate. Pure functions, no I/O."""

from app.enums import DocumentType
from app.models.document import Document
from app.models.extracted_field import ExtractedFieldRow
from app.processing import confidence


def _field(name, value, conf):
    return ExtractedFieldRow(name=name, value=value, confidence=conf)


def _doc(doc_type, classify_confidence):
    return Document(doc_type=doc_type, classify_confidence=classify_confidence)


# --- score: can only lower, never raise ---------------------------------------


def test_score_keeps_claude_confidence_when_the_validator_passes():
    assert confidence.score("member_id", "ABC-123", 0.9) == 0.9


def test_score_zeroes_out_a_validator_failure_regardless_of_claudes_confidence():
    assert confidence.score("member_id", "!!!", 0.99) == 0.0


def test_score_keeps_claude_confidence_for_a_field_with_no_validator():
    assert confidence.score("denial_reason", "anything goes", 0.7) == 0.7


def test_score_keeps_confidence_as_is_for_a_missing_value():
    """A missing field is scored 0.0 by the caller before this ever runs; score()
    itself must not try to validate None."""
    assert confidence.score("member_id", None, 0.0) == 0.0


def test_a_real_date_passes_its_validator():
    assert confidence.score("service_date", "2026-10-17", 0.8) == 0.8


def test_a_bogus_date_fails_its_validator():
    assert confidence.score("service_date", "not a date", 0.8) == 0.0


# --- all_ok: the gate -----------------------------------------------------------


def test_unknown_doc_type_always_fails_the_gate():
    doc = _doc(DocumentType.UNKNOWN, 1.0)
    assert confidence.all_ok(doc, []) is False


def test_low_classify_confidence_fails_the_gate_even_with_perfect_fields():
    doc = _doc(DocumentType.CLAIMS, 0.5)
    fields = [_field("member_id", "ABC-123", 1.0)]
    assert confidence.all_ok(doc, fields) is False


def test_one_low_confidence_field_fails_the_whole_gate():
    doc = _doc(DocumentType.CLAIMS, 0.95)
    fields = [_field("member_id", "ABC-123", 0.95), _field("claim_number", None, 0.0)]
    assert confidence.all_ok(doc, fields) is False


def test_every_field_above_threshold_passes_the_gate():
    doc = _doc(DocumentType.CLAIMS, 0.95)
    fields = [_field("member_id", "ABC-123", 0.95), _field("claim_number", "C1", 0.9)]
    assert confidence.all_ok(doc, fields) is True


def test_an_empty_fields_list_passes_vacuously():
    """correspondence and unknown have nothing to extract -- there is nothing to
    doubt, so a confident classification alone is enough."""
    doc = _doc(DocumentType.CORRESPONDENCE, 0.9)
    assert confidence.all_ok(doc, []) is True
