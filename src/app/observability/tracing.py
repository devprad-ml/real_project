''' Langfuse traces: what each question and document did, how long it took, what it cost.

Off until LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set. Question and document text
is only sent with LANGFUSE_CAPTURE_CONTENT=true, because it can be PHI and this is a third
party. Without it, traces still carry timings, tokens, cost and document ids.
'''

import uuid
from contextlib import contextmanager, nullcontext
from functools import lru_cache
from typing import Any, Iterator

from app.config import get_settings
from app.observability.logging import log


# Rather than a blank that looks like a broken trace.
NOT_CAPTURED = "[not captured: LANGFUSE_CAPTURE_CONTENT is off]"


def _only_ours(span) -> bool:
    ''' Libraries emit spans too once a tracer exists. The Anthropic SDK's would count every
    call's cost twice, and nothing here vouches for what those spans contain. '''
    scope = span.instrumentation_scope
    return scope is not None and scope.name == "langfuse-sdk"


@lru_cache
def client():
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key.get_secret_value(),
            base_url=s.langfuse_base_url,
            should_export_span=_only_ours,
            # The default of 5 s timed out on dataset writes; export happens in the background.
            timeout=30,
        )
    except Exception as exc:  # telemetry must not stop the app from starting
        log.warning("tracing_disabled", extra={"error_type": type(exc).__name__})
        return None


def content(value: Any) -> Any:
    if value is None or get_settings().langfuse_capture_content:
        return value
    return NOT_CAPTURED


@contextmanager
def observe(
    name: str,
    *,
    as_type: str = "span",
    tenant_id: uuid.UUID | None = None,
    input: Any = None,
    **fields: Any,
) -> Iterator[Any]:
    ''' One step of a trace, nested under whatever step is open. Yields None when tracing
    is off, so callers hand the result to `finish` and never check. Pass `tenant_id` only
    on the step that starts a trace: it names the trace and files it under that tenant.

    An exception is recorded by class only and raised after the span has closed, so the
    SDK never sees its message, which can quote document content. '''
    c = client()
    if c is None:
        yield None
        return

    from langfuse import propagate_attributes

    error: Exception | None = None
    with c.start_as_current_observation(
        name=name, as_type=as_type, input=content(input), **fields
    ) as observation:
        attributes = (
            propagate_attributes(user_id=str(tenant_id), trace_name=name)
            if tenant_id
            else nullcontext()
        )
        with attributes:
            if tenant_id:
                observation.set_trace_io(input=content(input))
            try:
                yield observation
            except Exception as exc:
                observation.update(level="ERROR", status_message=type(exc).__name__)
                error = exc
    if error is not None:
        raise error


def finish(
    observation: Any, *, output: Any = None, root: bool = False, **fields: Any
) -> None:
    if observation is None:
        return
    observation.update(output=content(output), **fields)
    if root:
        # The trace list shows trace-level output, not the first step's.
        observation.set_trace_io(output=content(output))


def shutdown() -> None:
    ''' Sends whatever is still buffered. '''
    c = client()
    if c is not None:
        c.shutdown()
