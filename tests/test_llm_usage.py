"""The spend ledger: one row per Claude call, written even when the job then fails."""

import time
import types

from sqlalchemy import select

from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind
from app.jobs import worker
from app.jobs.queue import enqueue
from app.models.llm_usage import LlmUsage
from app.processing import classify, llm


def _response(tin=120, tout=30):
    return types.SimpleNamespace(
        usage=types.SimpleNamespace(input_tokens=tin, output_tokens=tout)
    )


def _rows(tenant):
    with SessionLocal() as s:
        return s.scalars(select(LlmUsage).where(LlmUsage.tenant_id == tenant)).all()


def test_a_call_is_recorded_against_the_scoped_tenant_and_document(tenant, make_doc):
    doc = make_doc()
    token = llm.scope.set((tenant, doc.id))
    try:
        llm.record("classify", "claude-haiku-5-5", _response(), time.perf_counter())
    finally:
        llm.scope.reset(token)

    [row] = _rows(tenant)
    assert (row.tenant_id, row.document_id) == (tenant, doc.id)
    assert (row.purpose, row.model) == ("classify", "claude-haiku-5-5")
    assert (row.input_tokens, row.output_tokens) == (120, 30)
    assert row.latency_ms >= 0


def test_nothing_is_recorded_outside_a_scope_or_without_usage(tenant):
    llm.record("ask", "m", _response(), time.perf_counter())  # no scope

    token = llm.scope.set((tenant, None))
    try:
        llm.record("ask", "m", types.SimpleNamespace(), time.perf_counter())  # no usage
    finally:
        llm.scope.reset(token)

    assert _rows(tenant) == []


def test_a_recording_failure_never_breaks_the_call(tenant, monkeypatch):
    def broken():
        raise RuntimeError("ledger down")

    monkeypatch.setattr(llm, "SessionLocal", broken)
    token = llm.scope.set((tenant, None))
    try:
        llm.record("ask", "m", _response(), time.perf_counter())  # must not raise
    finally:
        llm.scope.reset(token)


def test_classify_records_its_call(tenant, make_doc, monkeypatch):
    """Through the real classify.run: the one-line hook is in the right place."""
    parsed = types.SimpleNamespace(
        stop_reason="end_turn",
        parsed_output=classify.ClassifyResult(
            doc_type=classify.DocumentType.CLAIMS, confidence=0.9
        ),
        usage=types.SimpleNamespace(input_tokens=700, output_tokens=40),
    )
    fake = types.SimpleNamespace(
        messages=types.SimpleNamespace(parse=lambda **kw: parsed)
    )
    monkeypatch.setattr(llm, "client", lambda: fake)
    doc = make_doc()

    token = llm.scope.set((tenant, doc.id))
    try:
        classify.run(["a claim form"])
    finally:
        llm.scope.reset(token)

    [row] = _rows(tenant)
    assert (row.purpose, row.input_tokens, row.output_tokens) == ("classify", 700, 40)


def test_the_spend_survives_the_job_failing_afterwards(tenant, make_doc, monkeypatch):
    """The handler's session is rolled back when the job fails. The ledger row was
    committed on its own session, because the money was spent either way."""
    doc = make_doc(DocumentStatus.RECEIVED)

    def handler(session, d):
        llm.record(
            "extract", "claude-sonnet-5-5", _response(900, 80), time.perf_counter()
        )
        raise RuntimeError("fell over after paying for the call")

    monkeypatch.setitem(worker.HANDLERS, JobKind.NORMALIZE, handler)
    with SessionLocal() as s:
        enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        s.commit()

    assert worker.run_once() is True

    [row] = _rows(tenant)
    assert (row.document_id, row.purpose) == (doc.id, "extract")
    assert row.input_tokens == 900


def test_the_scope_does_not_leak_between_jobs(tenant, make_doc, monkeypatch):
    """Reset after each job: a later call with no scope must not bill the last job."""
    doc = make_doc(DocumentStatus.RECEIVED)
    monkeypatch.setitem(worker.HANDLERS, JobKind.NORMALIZE, lambda s, d: None)
    with SessionLocal() as s:
        enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        s.commit()

    worker.run_once()

    assert llm.scope.get() is None
