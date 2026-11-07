''' The Anthropic client and input trimming shared by every Claude call. '''

from functools import lru_cache
from typing import Iterable

import anthropic

from app.config import get_settings
from app.observability import tracing


@lru_cache
def client() -> anthropic.Anthropic:
    # SDK retries 429/5xx/connection errors itself (2 retries); past that the
    # pipeline's retries take over.
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


def generation(purpose: str, model: str, messages: list):
    ''' Wrap one Claude call, and pass what it yields to `record`. '''
    return tracing.observe(purpose, as_type="generation", model=model, input=messages)


def record(traced, response, output=None) -> None:
    ''' Token usage and output onto the call's trace. Langfuse prices it from the model. '''
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    tracing.finish(
        traced,
        output=output,
        usage_details={"input": usage.input_tokens, "output": usage.output_tokens},
    )
