''' Confidence scoring and the review gate.

Per DESIGN.md: a heuristic that can only lower Claude's self-reported confidence,
never raise it -- the model's own number is already the optimistic case.
'''

import datetime
import re
from typing import Callable

from app.config import get_settings
from app.enums import DocumentType
from app.models.document import Document
from app.models.extracted_field import ExtractedFieldRow

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y")


def parse_date(value: str) -> datetime.date | None:
    ''' The one date parser: the validators below and MPI matching both use it. '''
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _is_a_real_date(value: str) -> bool:
    return parse_date(value) is not None


# A field with no entry here keeps Claude's confidence as-is -- not every field has
# a shape worth checking.
VALIDATORS: dict[str, Callable[[str], bool]] = {
    "member_id": lambda v: bool(re.fullmatch(r"[A-Za-z0-9-]{4,20}", v)),
    "service_date": _is_a_real_date,
    "dob": _is_a_real_date,
    "signed_date": _is_a_real_date,
    "billed_amount": lambda v: bool(re.fullmatch(r"\d+(\.\d{2})?", v)),
}


def score(name: str, value: str | None, confidence: float) -> float:
    ''' Claude's confidence, capped to 0 if a validated field fails its check. A
    missing value (expected but not found) or an unvalidated field name keeps
    whatever confidence it already has -- 0.0 for missing, Claude's own number
    otherwise. '''
    validator = VALIDATORS.get(name)
    if validator is None or value is None:
        return confidence
    return confidence if validator(value) else 0.0


def review_reasons(doc: Document, fields: list[ExtractedFieldRow]) -> list[str]:
    ''' The gate. Empty list keeps the document at EXTRACTED; anything else sends it
    to NEEDS_REVIEW and is what the reviewer is shown. A doc type classify wasn't
    sure about is never trusted here even if every field validates. An empty
    `fields` list adds no reasons: nothing was extracted to doubt. '''
    threshold = get_settings().confidence_threshold
    reasons = []
    if doc.doc_type is DocumentType.UNKNOWN:
        reasons.append("doc_type_unknown")
    elif doc.classify_confidence is None or doc.classify_confidence < threshold:
        reasons.append("classify_low_confidence")
    reasons += [
        f"field_low_confidence:{f.name}" for f in fields if f.confidence < threshold
    ]
    return reasons
