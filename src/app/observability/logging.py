''' One JSON object per log line, with PHI kept out structurally. See m13.md §3.1.

Only the keys in FIELDS are ever written. Anything else passed in `extra=` is
dropped, so `extra={"question": q}` cannot leak by accident. An exception is logged as
its class name only: the message (a Pydantic error echoes the bad value) and the
traceback text can carry document content. The full repr is in `jobs.last_error`, inside
the database. Logs say which, the row says what.
'''

import contextvars
import datetime
import json
import logging
import sys

from app.config import get_settings

FIELDS = (
    "request_id", "tenant_id", "principal_id", "document_id", "job_id", "kind",
    "status", "route", "method", "status_code", "duration_ms", "attempts", "model",
    "input_tokens", "output_tokens", "error_type",
)

# Set per request by the middleware; every line logged while serving it carries it.
request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

log = logging.getLogger("app")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        rid = request_id.get()
        if rid:
            out["request_id"] = rid
        for key in FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = str(value) if not isinstance(value, (int, float)) else value
        if record.exc_info and record.exc_info[0] is not None:
            out["error_type"] = record.exc_info[0].__name__
        return json.dumps(out, default=str)


def configure() -> None:
    ''' Idempotent: the API and the worker both call it at startup. '''
    if any(isinstance(h.formatter, JsonFormatter) for h in log.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(get_settings().log_level.upper())
    log.propagate = False  # not also through the root logger's plain formatter
