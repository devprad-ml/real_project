''' Normalized PDF text -> structured fields + a summary, via Claude.

One call does both jobs DESIGN.md splits in two (`extract.run`, `summarize.run`): the
summary draws on the same reading pass that found the fields, instead of re-reading
the document from scratch.

PHI leaves the boundary on every call. Production needs a BAA with Anthropic.
'''

from functools import lru_cache
from typing import Iterable

import anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.enums import DocumentType
from app.errors import PermanentError

SYSTEM = (
    "You extract structured fields and write a short summary for healthcare and "
    "insurance operational documents. Only use field names from the expected list "
    "you are given. If an expected field is not present in the document, omit it "
    "rather than guessing. Confidence is your probability, from 0 to 1, that the "
    "value you extracted is correct."
)

# Claims and consent forms don't share fields -- classify's result decides which
# names are even legal for a given document.
FIELD_SCHEMAS: dict[DocumentType, list[str]] = {
    DocumentType.CLAIMS: ["member_id", "claim_number", "service_date", "billed_amount"],
    DocumentType.DENIAL: ["member_id", "claim_number", "denial_code", "denial_reason"],
    DocumentType.APPEAL: ["member_id", "claim_number", "appeal_reason"],
    DocumentType.PRIOR_AUTH: ["member_id", "procedure_code", "service_date"],
    DocumentType.CONSENT: ["member_id", "signed_date"],
    DocumentType.MEDICAL_RECORD: ["member_id", "dob", "provider_name"],
    DocumentType.CORRESPONDENCE: [],  # too free-form to name fields in advance
    DocumentType.UNKNOWN: [],  # nothing to extract; the gate sends it to review anyway
}


class ExtractedField(BaseModel):
    name: str
    value: str
    confidence: float


class ExtractionResult(BaseModel):
    fields: list[ExtractedField]
    summary: str


EMPTY = ExtractionResult(fields=[], summary="")


@lru_cache
def _client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key.get_secret_value()
    return anthropic.Anthropic(api_key=key)


def _head(pages: Iterable[str], limit: int) -> str:
    ''' Leading text up to `limit` chars, same early-stop as classify's. '''
    parts: list[str] = []
    size = 0
    for text in pages:
        parts.append(text)
        size += len(text)
        if size >= limit:
            break
    return "\n\n".join(parts)[:limit].strip()


def run(doc_type: DocumentType, pages: Iterable[str]) -> ExtractionResult:
    ''' `pages` is page text in order, consumed lazily. '''
    s = get_settings()
    text = _head(pages, s.extract_max_chars)
    if not text:
        return EMPTY  # blank fax: nothing to send, and nothing to pay for

    expected = FIELD_SCHEMAS.get(doc_type, [])
    field_list = ", ".join(expected) if expected else "(none -- write a summary only)"

    try:
        response = _client().messages.parse(
            model=s.extract_model,
            max_tokens=2048,
            # Structured extraction, not open-ended reasoning.
            thinking={"type": "disabled"},
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Expected fields: {field_list}\n\n"
                        f"<document>\n{text}\n</document>"
                    ),
                }
            ],
            output_format=ExtractionResult,
        )
    except anthropic.BadRequestError as exc:
        # A 400 fails the same way on every retry.
        raise PermanentError(f"extract request rejected: {exc.message}") from exc

    # A refusal, or no parsable output, is a document a human should look at -- not
    # a job failure. Empty fields send it to the confidence gate with nothing to
    # vouch for, same as a doc type with no expected fields.
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return EMPTY
    result = response.parsed_output

    # A name Claude invents that isn't on this doc type's list is dropped (logged,
    # not stored) -- storing an unexpected column name is worse than losing one field.
    kept = [f for f in result.fields if f.name in expected]
    return ExtractionResult(
        fields=[
            ExtractedField(
                name=f.name, value=f.value, confidence=min(max(f.confidence, 0.0), 1.0)
            )
            for f in kept
        ],
        summary=result.summary,
    )
