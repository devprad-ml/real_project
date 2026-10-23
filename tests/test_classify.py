"""classify.run against a fake Anthropic client. No test calls the real API."""

import types

import anthropic
import httpx2
import pytest

from app.config import get_settings
from app.enums import DocumentType
from app.errors import PermanentError
from app.processing import classify


def reply(doc_type, confidence, stop_reason="end_turn"):
    parsed = classify.ClassifyResult(doc_type=doc_type, confidence=confidence)
    return types.SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed)


def test_returns_the_models_type_and_sends_only_the_head(fake_api):
    api = fake_api(reply(DocumentType.APPEAL, 0.88))
    limit = get_settings().classify_max_chars
    consumed = []

    def pages():
        for text in ["a" * limit, "never read"]:
            consumed.append(text)
            yield text

    result = classify.run(pages())

    assert result.doc_type == DocumentType.APPEAL
    assert result.confidence == 0.88
    [call] = api.calls
    assert call["model"] == get_settings().classify_model
    assert call["output_format"] is classify.ClassifyResult
    sent = call["messages"][0]["content"]
    assert "never read" not in sent
    # Page one already filled the budget, so page two was never even extracted.
    assert consumed == ["a" * limit]


def test_a_blank_document_is_unknown_without_an_api_call(fake_api):
    api = fake_api(reply(DocumentType.CLAIMS, 1.0))
    assert classify.run(["", "  \n "]) == classify.UNCLASSIFIED
    assert api.calls == []


def test_a_refusal_is_unknown_not_a_failure(fake_api):
    fake_api(reply(DocumentType.CLAIMS, 0.9, stop_reason="refusal"))
    assert classify.run(["some chart text"]) == classify.UNCLASSIFIED


def test_confidence_is_clamped_to_0_1(fake_api):
    fake_api(reply(DocumentType.DENIAL, 1.7))
    assert classify.run(["text"]).confidence == 1.0


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
        classify.run(["text"])
