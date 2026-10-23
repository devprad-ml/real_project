"""extract.run against a fake Anthropic client. No test calls the real API."""

import types

import anthropic
import httpx2
import pytest

from app.config import get_settings
from app.enums import DocumentType
from app.errors import PermanentError
from app.processing import extract


def reply(fields, summary, stop_reason="end_turn"):
    parsed = extract.ExtractionResult(
        fields=[extract.ExtractedField(**f) for f in fields], summary=summary
    )
    return types.SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed)


def test_sends_the_expected_field_list_and_returns_fields_plus_summary(fake_api):
    api = fake_api(
        reply(
            [{"name": "member_id", "value": "ABC-123", "confidence": 0.9}],
            "A claim for a routine visit.",
        )
    )

    result = extract.run(DocumentType.CLAIMS, ["a page of claim text"])

    assert [f.name for f in result.fields] == ["member_id"]
    assert result.fields[0].value == "ABC-123"
    assert result.summary == "A claim for a routine visit."
    [call] = api.calls
    assert call["model"] == get_settings().extract_model
    assert call["output_format"] is extract.ExtractionResult
    sent = call["messages"][0]["content"]
    assert "member_id" in sent and "claim_number" in sent


def test_sends_only_the_head_of_a_long_document(fake_api):
    api = fake_api(reply([], "s"))
    limit = get_settings().extract_max_chars

    extract.run(DocumentType.CLAIMS, ["a" * limit, "never read"])

    assert "never read" not in api.calls[0]["messages"][0]["content"]


def test_a_blank_document_is_empty_without_an_api_call(fake_api):
    api = fake_api(reply([{"name": "member_id", "value": "x", "confidence": 1.0}], "s"))
    assert extract.run(DocumentType.CLAIMS, ["", "  \n "]) == extract.EMPTY
    assert api.calls == []


def test_a_refusal_is_none_not_empty_and_not_a_failure(fake_api):
    """None, not EMPTY: a blank page and a document Claude would not read are
    different outcomes, and only the second one needs a human."""
    fake_api(reply([], "s", stop_reason="refusal"))
    assert extract.run(DocumentType.CLAIMS, ["some document text"]) is None


def test_confidence_is_clamped_to_0_1(fake_api):
    fake_api(reply([{"name": "member_id", "value": "x", "confidence": 1.7}], "s"))
    result = extract.run(DocumentType.CLAIMS, ["text"])
    assert result.fields[0].confidence == 1.0


def test_a_rejected_request_is_permanent(fake_api):
    """A 400 fails identically on every retry, so it must not burn MAX_ATTEMPTS."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError(
        "prompt is too long",
        response=httpx2.Response(400, request=request),
        body=None,
    )
    fake_api(error=error)

    with pytest.raises(PermanentError):
        extract.run(DocumentType.CLAIMS, ["text"])


def test_a_type_with_no_expected_fields_still_gets_a_summary(fake_api):
    api = fake_api(reply([], "A letter about a billing dispute."))

    result = extract.run(DocumentType.CORRESPONDENCE, ["dear sir or madam..."])

    assert result.fields == []
    assert result.summary == "A letter about a billing dispute."
    assert "none" in api.calls[0]["messages"][0]["content"].lower()
