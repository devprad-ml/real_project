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


def _is_a_real_date(value: str) -> bool:
    for fmt in _DATE_FORMATS:
        try:
            datetime.datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


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


def all_ok(doc: Document, fields: list[ExtractedFieldRow]) -> bool:
    ''' The gate: True keeps the document at EXTRACTED, False sends it to
    NEEDS_REVIEW. Doesn't re-score classification -- a doc type M6 wasn't sure
    about is never trusted here even if every individual field happens to
    validate. An empty `fields` list passes vacuously: nothing was extracted to
    doubt. '''
    settings = get_settings()
    if doc.doc_type is DocumentType.UNKNOWN:
        return False
    if (
        doc.classify_confidence is None
        or doc.classify_confidence < settings.confidence_threshold
    ):
        return False
    return all(f.confidence >= settings.confidence_threshold for f in fields)
