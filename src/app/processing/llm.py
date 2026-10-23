''' The Anthropic client and input trimming shared by every Claude call. '''

from functools import lru_cache
from typing import Iterable

import anthropic

from app.config import get_settings


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
