"""Tracing: off without keys, keeps content out by default, never changes behaviour."""

import types

import pytest

from app.config import get_settings
from app.observability import tracing
from app.rag import llm


class _Observation:
    def __init__(self, record):
        self.record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.record["exception_reached_sdk"] = exc[0] is not None
        return False

    def update(self, **fields):
        self.record.setdefault("updates", []).append(fields)

    def set_trace_io(self, **fields):
        self.record.setdefault("trace_io", []).append(fields)


class _Client:
    def __init__(self):
        self.started = []

    def start_as_current_observation(self, **fields):
        record = dict(fields)
        self.started.append(record)
        return _Observation(record)


@pytest.fixture
def langfuse(monkeypatch):
    fake = _Client()
    monkeypatch.setattr(tracing, "client", lambda: fake)
    return fake


def test_without_keys_it_is_off_and_steps_still_run():
    tracing.client.cache_clear()
    assert tracing.client() is None
    with tracing.observe("step", input="Jane Roe") as observation:
        tracing.finish(observation, output="anything")
    assert observation is None


def test_content_stays_out_by_default(langfuse):
    with tracing.observe("step", input="Jane Roe") as observation:
        tracing.finish(observation, output="diabetes", metadata={"hits": 2})

    [step] = langfuse.started
    assert step["input"] == tracing.NOT_CAPTURED
    assert step["updates"] == [{"output": tracing.NOT_CAPTURED, "metadata": {"hits": 2}}]


def test_a_root_step_sets_the_trace_input_and_output(langfuse, tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "langfuse_capture_content", True)

    with tracing.observe("ask", tenant_id=tenant, input="question") as observation:
        tracing.finish(observation, output="answer", root=True)

    [step] = langfuse.started
    assert step["trace_io"] == [{"input": "question"}, {"output": "answer"}]


def test_a_nested_step_leaves_the_trace_alone(langfuse):
    with tracing.observe("retrieve", input="query") as observation:
        tracing.finish(observation, output="hits")

    [step] = langfuse.started
    assert "trace_io" not in step


def test_only_our_own_spans_are_exported():
    def span(name):
        return types.SimpleNamespace(instrumentation_scope=types.SimpleNamespace(name=name))

    assert tracing._only_ours(span("langfuse-sdk"))
    assert not tracing._only_ours(span("com.anthropic.sdk.python"))
    assert not tracing._only_ours(types.SimpleNamespace(instrumentation_scope=None))


def test_content_is_sent_when_turned_on(langfuse, monkeypatch):
    monkeypatch.setattr(get_settings(), "langfuse_capture_content", True)

    with tracing.observe("step", input="question") as observation:
        tracing.finish(observation, output="answer")

    [step] = langfuse.started
    assert step["input"] == "question"
    assert step["updates"] == [{"output": "answer"}]


def test_an_error_keeps_its_class_and_never_its_message(langfuse):
    with pytest.raises(ValueError, match="Jane Roe"):
        with tracing.observe("step"):
            raise ValueError("Jane Roe is not valid")

    [step] = langfuse.started
    assert step["updates"] == [{"level": "ERROR", "status_message": "ValueError"}]
    assert step["exception_reached_sdk"] is False


def test_a_tenant_is_attached_to_the_trace(langfuse, tenant):
    with tracing.observe("step", tenant_id=tenant):
        pass

    assert len(langfuse.started) == 1


def test_a_claude_call_puts_its_usage_and_output_on_its_trace(langfuse):
    response = types.SimpleNamespace(
        usage=types.SimpleNamespace(input_tokens=2400, output_tokens=310)
    )

    with llm.generation("answer", "claude-haiku-5-5", [{"role": "user"}]) as traced:
        llm.record(traced, response, "the answer")

    [step] = langfuse.started
    assert (step["as_type"], step["model"]) == ("generation", "claude-haiku-5-5")
    assert step["updates"] == [
        {
            "output": tracing.NOT_CAPTURED,
            "usage_details": {"input": 2400, "output": 310},
        }
    ]
