"""M10: push, the signed webhook, requeue, and the mock downstream."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import mock_downstream
from app.config import get_settings
from app.db import SessionLocal
from app.enums import (
    AuditAction,
    DocumentStatus,
    DocumentType,
    JobKind,
    JobState,
    Role,
    RouteQueue,
)
from app.errors import PermanentError
from app.integrations import downstream, signing
from app.jobs import handlers
from app.jobs.queue import enqueue
from app.models import Association, Document, ExtractedFieldRow, Job, Log, Member
from app.storage.blob import normalized_key

PDF = b"%PDF-1.7 routed"
SECRET = get_settings().downstream_webhook_secret.get_secret_value()


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def routed_doc(tenant, make_doc, blob):
    """Factory: a ROUTED claim, bound to member ABC123 unless `bound=False`."""

    def _make(bound=True, status=DocumentStatus.ROUTED):
        doc = make_doc(status)
        with SessionLocal() as s:
            d = s.get(Document, doc.id)
            d.doc_type = DocumentType.CLAIMS
            d.route_queue = RouteQueue.CLAIMS_OPS
            d.summary = "A claim."
            d.normalized_blob_key = normalized_key(tenant, doc.id, DocumentType.CLAIMS)
            blob.put(d.normalized_blob_key, PDF, "application/pdf")
            s.add(
                ExtractedFieldRow(
                    tenant_id=tenant,
                    document_id=doc.id,
                    name="claim_number",
                    value="C-77",
                    confidence=0.9,
                )
            )
            if bound:
                m = Member(tenant_id=tenant, member_id="ABC123")
                s.add(m)
                s.flush()
                s.add(
                    Association(
                        tenant_id=tenant,
                        document_id=doc.id,
                        member_pk=m.id,
                        match_confidence=0.9,
                    )
                )
            s.commit()
        return doc

    return _make


@pytest.fixture
def downstream_says(monkeypatch):
    """Point push at an in-process fake. Set `.status` (or `.raise_`) to choose its
    answer; `.requests` records what it was sent."""

    class Fake:
        status = 202
        raise_ = None
        requests: list[httpx.Request] = []

        def handle(self, request):
            self.requests.append(request)
            if self.raise_:
                raise self.raise_
            return httpx.Response(self.status, json={"id": "ds-1"})

    fake = Fake()
    fake.requests = []
    monkeypatch.setattr(
        downstream,
        "client",
        lambda: httpx.Client(
            base_url="http://downstream.test", transport=httpx.MockTransport(fake.handle)
        ),
    )
    return fake


def push(doc):
    with SessionLocal() as s:
        handlers.handle_push(s, s.get(Document, doc.id))
        s.commit()


def latest_log(doc_id):
    with SessionLocal() as s:
        return s.scalars(
            select(Log).where(Log.document_id == doc_id).order_by(Log.at.desc())
        ).first()


def metadata_of(request: httpx.Request) -> dict:
    # Multipart body: pull the JSON part out by its field name.
    body = request.content.decode("latin-1")
    part = body.split('name="metadata"', 1)[1].split("\r\n\r\n", 1)[1]
    return json.loads(part.split("\r\n--", 1)[0])


# --- push -----------------------------------------------------------------------


def test_push_sends_metadata_and_the_pdf_then_marks_pushed(routed_doc, downstream_says):
    doc = routed_doc()

    push(doc)

    [req] = downstream_says.requests
    assert req.url.path == "/documents"
    assert req.headers["Idempotency-Key"] == f"{doc.id}:0"
    meta = metadata_of(req)
    assert meta["external_id"] == str(doc.id)
    assert meta["member_id"] == "ABC123"
    assert meta["queue"] == "claims_ops"
    assert meta["fields"] == {"claim_number": "C-77"}
    assert PDF in req.content
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.PUSHED
    assert latest_log(doc.id).detail == {
        "to": "PUSHED",
        "idempotency_key": f"{doc.id}:0",
        "status_code": 202,
    }


def test_an_unbound_document_pushes_a_null_member(routed_doc, downstream_says):
    doc = routed_doc(bound=False)
    push(doc)
    assert metadata_of(downstream_says.requests[0])["member_id"] is None


@pytest.mark.parametrize("status", [500, 503, 408, 429])
def test_transient_answers_raise_and_leave_it_routed(routed_doc, downstream_says, status):
    downstream_says.status = status
    doc = routed_doc()

    with pytest.raises(downstream.DownstreamUnavailable):
        push(doc)

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.ROUTED


def test_a_timeout_is_retried_like_any_other_error(routed_doc, downstream_says):
    downstream_says.raise_ = httpx.ConnectTimeout("slow")
    with pytest.raises(httpx.TransportError):
        push(routed_doc())


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_other_4xx_is_permanent(routed_doc, downstream_says, status):
    downstream_says.status = status
    with pytest.raises(PermanentError):
        push(routed_doc())


def test_a_retry_reuses_the_key(routed_doc, downstream_says):
    """Crash after the downstream accepted, before our commit: same key, so the
    downstream dedupes instead of filing it twice."""
    doc = routed_doc()
    with SessionLocal() as s:
        handlers.handle_push(s, s.get(Document, doc.id))
        s.rollback()  # the crash
    push(doc)

    keys = [r.headers["Idempotency-Key"] for r in downstream_says.requests]
    assert keys == [f"{doc.id}:0", f"{doc.id}:0"]


def test_a_repush_after_rejection_gets_a_new_key(routed_doc, downstream_says):
    doc = routed_doc()
    push(doc)
    with SessionLocal() as s:  # rejected, reviewed, back through route
        s.get(Document, doc.id).status = DocumentStatus.ROUTED
        s.commit()
    push(doc)

    assert downstream_says.requests[1].headers["Idempotency-Key"] == f"{doc.id}:1"


def test_push_is_a_noop_unless_routed(routed_doc, downstream_says):
    doc = routed_doc(status=DocumentStatus.PUSHED)
    push(doc)
    assert downstream_says.requests == []


def test_route_hands_over_to_push_through_the_worker_registry():
    assert handlers.HANDLERS[JobKind.PUSH] is handlers.handle_push


def test_every_handler_has_a_requeue_precondition():
    assert set(handlers.REQUIRES) == set(handlers.HANDLERS)


# --- signing --------------------------------------------------------------------


def test_signing_round_trips():
    ts = str(int(time.time()))
    assert signing.verify(SECRET, ts, b"{}", signing.sign(SECRET, ts, b"{}"))


@pytest.mark.parametrize(
    ("ts_offset", "body", "secret", "sig"),
    [
        (0, b'{"x": 2}', SECRET, None),  # body changed after signing
        (0, b"{}", "wrong-secret", None),  # signed with another secret
        (-301, b"{}", SECRET, None),  # outside the replay window
        (0, b"{}", SECRET, "sha256=é"),  # junk, non-ASCII: False, not a crash
    ],
)
def test_signing_rejects(ts_offset, body, secret, sig):
    ts = str(int(time.time()) + ts_offset)
    signature = sig or signing.sign(secret, ts, b"{}")
    assert not signing.verify(SECRET, ts, body, signature)


def test_a_non_numeric_timestamp_is_rejected():
    assert not signing.verify(SECRET, "soon", b"{}", signing.sign(SECRET, "soon", b"{}"))


# --- webhook --------------------------------------------------------------------


def post_verdict(client, payload, *, secret=SECRET, ts=None):
    body = json.dumps(payload).encode()
    ts = ts or str(int(time.time()))
    return client.post(
        "/webhooks/downstream",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": signing.sign(secret, ts, body),
        },
    )


def test_accepted_is_recorded_and_changes_nothing(client, routed_doc):
    doc = routed_doc(status=DocumentStatus.PUSHED)

    r = post_verdict(client, {"external_id": str(doc.id), "status": "accepted"})

    assert r.status_code == 204
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.PUSHED
    log = latest_log(doc.id)
    assert (log.action, log.actor) == (AuditAction.VERDICT, "downstream")


def test_rejected_sends_it_to_review_with_the_reason(client, routed_doc, staff):
    doc = routed_doc(status=DocumentStatus.PUSHED)

    r = post_verdict(
        client,
        {"external_id": str(doc.id), "status": "rejected", "reason": "bad claim no"},
    )

    assert r.status_code == 204
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
    # and the review queue shows why
    [item] = client.get("/validation", headers=staff(Role.REVIEWER)).json()
    assert item["reasons"] == ["downstream_rejected: bad claim no"]


def test_a_late_rejection_is_recorded_only(client, routed_doc):
    doc = routed_doc(status=DocumentStatus.NEEDS_REVIEW)

    r = post_verdict(client, {"external_id": str(doc.id), "status": "rejected"})

    assert r.status_code == 204
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
    assert latest_log(doc.id).action == AuditAction.VERDICT


def test_a_verdict_that_beats_the_push_commit_is_a_409(client, routed_doc):
    doc = routed_doc()
    r = post_verdict(client, {"external_id": str(doc.id), "status": "accepted"})
    assert r.status_code == 409


def test_a_bad_signature_is_a_401_and_writes_nothing(client, routed_doc):
    doc = routed_doc(status=DocumentStatus.PUSHED)

    r = post_verdict(
        client, {"external_id": str(doc.id), "status": "rejected"}, secret="nope"
    )

    assert r.status_code == 401
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.PUSHED
    assert latest_log(doc.id) is None


def test_a_stale_timestamp_is_a_401(client, routed_doc):
    doc = routed_doc(status=DocumentStatus.PUSHED)
    stale = str(int(time.time()) - 3600)
    r = post_verdict(client, {"external_id": str(doc.id), "status": "x"}, ts=stale)
    assert r.status_code == 401


def test_an_unknown_document_is_a_404(client):
    r = post_verdict(
        client,
        {"external_id": "00000000-0000-0000-0000-000000000000", "status": "accepted"},
    )
    assert r.status_code == 404


def test_a_signed_but_malformed_body_is_a_422(client):
    assert post_verdict(client, {"status": "maybe"}).status_code == 422


# --- requeue --------------------------------------------------------------------


@pytest.fixture
def failed_push(tenant, routed_doc):
    doc = routed_doc(status=DocumentStatus.FAILED)
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.PUSH)
        job.state = JobState.ERROR
        job.attempts = 30
        s.commit()
    return doc


def test_requeue_restarts_the_failed_step(client, staff, failed_push):
    r = client.post(
        f"/admin/documents/{failed_push.id}/requeue", headers=staff(Role.ADMIN)
    )

    assert r.status_code == 202
    with SessionLocal() as s:
        assert s.get(Document, failed_push.id).status == DocumentStatus.ROUTED
        pending = s.scalars(
            select(Job).where(
                Job.document_id == failed_push.id, Job.state == JobState.PENDING
            )
        ).all()
        assert [(j.kind, j.attempts) for j in pending] == [(JobKind.PUSH, 0)]
    log = latest_log(failed_push.id)
    assert log.action == AuditAction.REQUEUE
    assert log.detail == {"kind": "push", "from": "FAILED", "to": "ROUTED"}


def test_requeue_needs_admin(client, staff, failed_push):
    r = client.post(
        f"/admin/documents/{failed_push.id}/requeue", headers=staff(Role.REVIEWER)
    )
    assert r.status_code == 403


def test_requeue_of_a_healthy_document_is_a_409(client, staff, routed_doc):
    doc = routed_doc()
    r = client.post(f"/admin/documents/{doc.id}/requeue", headers=staff(Role.ADMIN))
    assert r.status_code == 409


def test_requeue_of_another_tenants_document_is_a_404(
    client, staff, failed_push, other_tenant
):
    headers = staff(Role.ADMIN, tenant_id=other_tenant)
    r = client.post(f"/admin/documents/{failed_push.id}/requeue", headers=headers)
    assert r.status_code == 404


# --- mock downstream ------------------------------------------------------------


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setattr(mock_downstream, "seen", {})
    sent = []
    monkeypatch.setattr(mock_downstream, "send_verdict", sent.append)
    with TestClient(mock_downstream.app) as c:
        yield c, sent


def mock_push(c, key, member_id="ABC123"):
    meta = {"external_id": "d1", "member_id": member_id}
    return c.post(
        "/documents",
        data={"metadata": json.dumps(meta)},
        files={"file": ("claims.pdf", PDF, "application/pdf")},
        headers={"Idempotency-Key": key},
    )


def test_the_mock_dedupes_a_replayed_key(mock):
    c, sent = mock
    first = mock_push(c, "k1").json()
    assert mock_push(c, "k1").json() == first
    assert len(sent) == 1  # one verdict, not two


def test_the_mock_rejects_an_unbound_member(mock):
    c, sent = mock
    mock_push(c, "k1", member_id=None)
    assert sent[0]["status"] == "rejected"


def test_the_mock_outage_switch(mock, monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_RATE", "1")
    assert mock_push(mock[0], "k1").status_code == 503


def test_the_mocks_signed_callback_passes_our_receiver(client, routed_doc, monkeypatch):
    """Sender and receiver share `sign`, and this proves they agree end to end."""
    doc = routed_doc(status=DocumentStatus.PUSHED)
    monkeypatch.setattr(mock_downstream.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        mock_downstream.httpx,
        "post",
        lambda url, content, headers, timeout: client.post(
            "/webhooks/downstream", content=content, headers=headers
        ),
    )

    mock_downstream.send_verdict(
        {"external_id": str(doc.id), "status": "rejected", "reason": "r"}
    )

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
