''' The Anthropic client and input trimming shared by every Claude call. '''

import contextvars
import time
import uuid
from functools import lru_cache
from typing import Iterable

import anthropic

from app.config import get_settings
from app.db import SessionLocal
from app.models.llm_usage import LlmUsage
from app.observability.logging import log

# (tenant_id, document_id) of the work being paid for. The worker sets it per job and
# /ask per request, so a Claude call needs no extra arguments to be attributed.
scope: contextvars.ContextVar[tuple[uuid.UUID, uuid.UUID | None] | None] = (
    contextvars.ContextVar("llm_scope", default=None)
)


@lru_cache
def client() -> anthropic.Anthropic:
    # SDK retries 429/5xx/connection errors itself (2 retries); past that the job
    # queue's backoff takes over.
    key = get_settings().anthropic_api_key.get_secret_value()
    return anthropic.Anthropic(api_key=key)


def head(pages: Iterable[str], limit: int) -> str:
    ''' Leading text up to `limit` chars. Stops consuming `pages` once the budget is
    full, so a 500-page record never costs 500 pages of extraction or tokens. '''
    parts: list[str] = []
    size = 0
    for text in pages:
        parts.append(text)
        size += len(text)
        if size >= limit:
            break
    return "\n\n".join(parts)[:limit].strip()


def record(purpose: str, model: str, response, started: float) -> None:
    ''' Ledger row for one Claude call that returned (m13.md §3.3).

    Its own session and commit, never the caller's: a failing job rolls its session
    back, but the money was spent either way, and a rolled-back ledger would
    under-count exactly the expensive failures. Telemetry must not fail a request or a
    job, so a recording error is logged and swallowed. '''
    ctx = scope.get()
    usage = getattr(response, "usage", None)
    if ctx is None or usage is None:
        return
    try:
        with SessionLocal() as session:
            session.add(
                LlmUsage(
                    tenant_id=ctx[0],
                    document_id=ctx[1],
                    purpose=purpose,
                    model=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            session.commit()
    except Exception as exc:
        log.warning("llm_usage_not_recorded", extra={"error_type": type(exc).__name__})
