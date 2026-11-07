"""Logging: JSON lines, PHI kept out structurally, one request line, a request id."""

import io
import json
import logging

import pytest

from app.enums import Role
from app.ingestion import pipeline
from app.observability.logging import JsonFormatter, log


@pytest.fixture
def lines():
    """Capture what `app` loggers write, parsed. Restores the logger afterwards."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    log.removeHandler(handler)
    log.setLevel(old_level)


def test_only_allowlisted_fields_are_written(lines):
    log.info("evt", extra={"document_id": "d-1", "question": "Jane Roe diagnosis"})

    [line] = lines()
    assert line["document_id"] == "d-1"
    assert "question" not in line
    assert "Jane Roe" not in json.dumps(line)


def test_an_exception_is_logged_as_its_class_only(lines):
    try:
        raise ValueError("member Jane Roe dob 1980-04-12")
    except ValueError:
        log.exception("boom")

    [line] = lines()
    assert line["error_type"] == "ValueError"
    assert "Jane" not in json.dumps(line)
    assert "Traceback" not in json.dumps(line)


def test_a_failing_document_logs_the_error_class_not_what_the_error_said(
    make_doc, monkeypatch, lines
):
    def run(doc):
        raise ValueError("Jane Roe 1980-04-12 is not a valid member id")

    monkeypatch.setattr(pipeline, "_run", run)
    doc = make_doc()

    pipeline.process(doc.id)

    [line] = [x for x in lines() if x["event"] == "ingest_retry"]
    assert line["error_type"] == "ValueError"
    assert line["document_id"] == str(doc.id)
    assert "Jane" not in json.dumps(line)


def test_a_request_logs_its_route_not_its_query(client, lines):
    r = client.get("/healthz?q=jane+roe+diabetes", headers={"X-Request-ID": "req-abc"})

    assert r.headers["X-Request-ID"] == "req-abc"
    [line] = [x for x in lines() if x["event"] == "request"]
    assert line["route"] == "/healthz"
    assert line["status_code"] == 200
    assert line["request_id"] == "req-abc"
    assert "jane" not in json.dumps(lines()).lower()


def test_a_request_without_an_id_gets_one(client):
    assert len(client.get("/healthz").headers["X-Request-ID"]) == 32


def test_the_request_line_carries_the_tenant(client, staff, tenant, lines):
    client.get("/admin/stats", headers=staff(Role.ADMIN))

    [line] = [x for x in lines() if x["event"] == "request"]
    assert line["route"] == "/admin/stats"
    assert line["tenant_id"] == str(tenant)


def test_a_route_template_is_logged_not_the_raw_path(client, staff, lines):
    client.get(
        "/documents/00000000-0000-0000-0000-000000000000/file", headers=staff()
    )

    [line] = [x for x in lines() if x["event"] == "request"]
    assert line["route"] == "/documents/{document_id}/file"
    assert line["status_code"] == 404
