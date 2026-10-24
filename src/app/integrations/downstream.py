''' Push a routed document to the downstream operational system (Facets/QNXT in the
BRD; `app.mock_downstream` locally).

No retry loop in here: the job queue is the one retry layer (m10.md §2). This module
only decides which failures are worth retrying.
'''

import json

import httpx

from app.config import get_settings
from app.errors import PermanentError

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# 4xx that mean "later", not "never".
RETRYABLE_4XX = {408, 429}


class DownstreamUnavailable(Exception):
    ''' Transient: the worker retries it with backoff. '''


def client() -> httpx.Client:
    # ponytail: a client per push; a pooled module-level one when push volume makes
    # connection setup show up in the latency.
    return httpx.Client(base_url=get_settings().downstream_base_url, timeout=TIMEOUT)


def push(metadata: dict, pdf: bytes, filename: str, *, idempotency_key: str) -> int:
    ''' Returns the downstream's 2xx status code. Transport errors and timeouts
    propagate as httpx exceptions, which the worker retries like any other. '''
    with client() as c:
        r = c.post(
            "/documents",
            data={"metadata": json.dumps(metadata)},
            files={"file": (filename, pdf, "application/pdf")},
            headers={"Idempotency-Key": idempotency_key},
        )
    if r.is_success:
        return r.status_code
    # Status code only: the response body may echo PHI back, and this text lands in
    # jobs.last_error and the audit log.
    if r.status_code >= 500 or r.status_code in RETRYABLE_4XX:
        raise DownstreamUnavailable(f"downstream returned {r.status_code}")
    raise PermanentError(f"downstream refused the push: {r.status_code}")
