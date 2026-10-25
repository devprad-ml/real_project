''' Document text -> document type, via Claude.

The type picks the normalized PDF's file name (`claims.pdf`) and rides in every vector
payload as a search filter, so it has to come from a closed list: the structured
output schema is the `DocumentType` enum, and the model cannot invent a type.

PHI leaves the boundary on every call. Production needs a BAA with Anthropic.
'''

import time
from typing import Iterable

import anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.enums import DocumentType
from app.errors import PermanentError
from app.processing import llm

SYSTEM = (
    "You classify healthcare and insurance operational documents received by a health "
    "plan. Pick the single best document type for the document you are given. Use "
    "'unknown' when no type fits or the text is too garbled to tell. Confidence is "
    "your probability, from 0 to 1, that the type is correct."
)


class ClassifyResult(BaseModel):
    doc_type: DocumentType
    confidence: float


UNCLASSIFIED = ClassifyResult(doc_type=DocumentType.UNKNOWN, confidence=0.0)


def run(pages: Iterable[str]) -> ClassifyResult:
    ''' `pages` is page text in order, consumed lazily. A document announces its type
    on page one, so only the head is sent. '''
    s = get_settings()
    text = llm.head(pages, s.classify_max_chars)
    if not text:
        return UNCLASSIFIED  # blank fax: nothing to send, and nothing to pay for

    started = time.perf_counter()
    try:
        response = llm.client().messages.parse(
            model=s.classify_model,
            max_tokens=256,
            # One label from a fixed list; thinking would only add latency and cost.
            thinking={"type": "disabled"},
            system=SYSTEM,
            messages=[
                {"role": "user", "content": f"<document>\n{text}\n</document>"}
            ],
            output_format=ClassifyResult,
        )
    except anthropic.BadRequestError as exc:
        # A 400 fails the same way on every retry.
        raise PermanentError(f"classify request rejected: {exc.message}") from exc

    llm.record("classify", s.classify_model, response, started)

    # A refusal, or no parsable output, is a document a human should look at -- not
    # a job failure. UNKNOWN with confidence 0 is what the M7 review gate catches.
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return UNCLASSIFIED
    result = response.parsed_output
    return ClassifyResult(
        doc_type=result.doc_type, confidence=min(max(result.confidence, 0.0), 1.0)
    )
