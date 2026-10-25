"""GET /admin/stats: what is queued, stuck, failed and spent -- for one tenant."""

import datetime
import uuid

import pytest

from app.db import SessionLocal
from app.enums import (
    AuditAction, DocumentStatus, DocumentType, JobKind, JobState, Role,
)
from app.jobs.queue import _utcnow
from app.models import Document, Job, Log
from app.models.llm_usage import LlmUsage
from app.observability.stats import est_usd


def _job(tenant, doc, kind, state, **kw):
    return Job(tenant_id=tenant, document_id=doc.id, kind=kind, state=state, **kw)


def _usage(tenant, purpose, model, tin, tout, ms, age_hours=0):
    return LlmUsage(
        tenant_id=tenant,
        document_id=None,
        purpose=purpose,
        model=model,
        input_tokens=tin,
        output_tokens=tout,
        latency_ms=ms,
        created_at=_utcnow() - datetime.timedelta(hours=age_hours),
    )


@pytest.fixture
def seeded(tenant, other_tenant, make_doc):
    """A tenant with a known mix, and another tenant whose rows must not show up."""
    pushed = make_doc(DocumentStatus.PUSHED)
    make_doc(DocumentStatus.PUSHED)
    make_doc(DocumentStatus.NEEDS_REVIEW)
    make_doc(DocumentStatus.FAILED)
    now = _utcnow()
    with SessionLocal() as s:
        s.add_all(
            [
                _job(tenant, pushed, JobKind.EMBED, JobState.PENDING,
                     run_after=now - datetime.timedelta(seconds=120)),
                # Backing off, not yet due: waiting by design, so not lag.
                _job(tenant, pushed, JobKind.PUSH, JobState.PENDING,
                     run_after=now + datetime.timedelta(hours=1)),
                _job(tenant, pushed, JobKind.CLASSIFY, JobState.RUNNING,
                     locked_at=now - datetime.timedelta(hours=1)),  # dead worker
                _job(tenant, pushed, JobKind.EXTRACT, JobState.RUNNING, locked_at=now),
                _job(tenant, pushed, JobKind.PUSH, JobState.ERROR),
                _job(tenant, pushed, JobKind.ROUTE, JobState.DONE),
            ]
        )
        for at in (now, now, now - datetime.timedelta(hours=2)):
            s.add(
                Log(tenant_id=tenant, document_id=pushed.id, actor="system",
                    action=AuditAction.STATE_CHANGE, detail={"to": "PUSHED"}, at=at)
            )
        s.add_all(
            [
                _usage(tenant, "classify", "claude-haiku-5-5", 1000, 100, 1000),
                _usage(tenant, "classify", "claude-haiku-5-5", 3000, 300, 3000),
                _usage(tenant, "extract", "claude-sonnet-5-5", 5000, 500, 4000),
                _usage(tenant, "ask", "claude-future-9", 10, 10, 10),
                _usage(tenant, "ask", "claude-haiku-5-5", 1, 1, 1, age_hours=25),
            ]
        )
        # Another tenant's data, which must not appear.
        other_doc = Document(
            tenant_id=other_tenant, sha256=uuid.uuid4().hex * 2,
            doc_type=DocumentType.UNKNOWN, blob_key="raw/x", status=DocumentStatus.FAILED,
        )
        s.add(other_doc)
        s.flush()
        s.add(_job(other_tenant, other_doc, JobKind.EMBED, JobState.ERROR))
        s.add(_usage(other_tenant, "ask", "claude-haiku-5-5", 99999, 99999, 99999))
        s.commit()


def test_the_numbers_for_a_known_tenant(client, staff, seeded):
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()

    assert body["documents_by_status"] == {"PUSHED": 2, "NEEDS_REVIEW": 1, "FAILED": 1}
    assert body["jobs"]["embed"] == {"pending": 1, "running": 0, "error": 0}
    assert body["jobs"]["push"] == {"pending": 1, "running": 0, "error": 1}
    assert body["jobs"]["route"] == {"pending": 0, "running": 0, "error": 0}  # done
    assert body["dead_letter"] == 1
    assert body["running_jobs"] == 2
    assert body["stale_running"] == 1
    assert list(body["oldest_due_pending_s"]) == ["embed"]  # the backoff job is not lag
    assert 119 <= body["oldest_due_pending_s"]["embed"] <= 130
    assert body["transitions_last_hour"] == {"PUSHED": 2}  # the 2h-old row is out


def test_llm_spend_by_purpose_and_model_over_24_hours(client, staff, seeded):
    llm = {
        (r["purpose"], r["model"]): r
        for r in client.get("/admin/stats", headers=staff(Role.ADMIN)).json()["llm_24h"]
    }

    haiku = llm[("classify", "claude-haiku-5-5")]
    assert haiku["calls"] == 2
    assert (haiku["input_tokens"], haiku["output_tokens"]) == (4000, 400)
    assert (haiku["p50_ms"], haiku["p95_ms"]) == (2000, 2900)
    assert haiku["est_usd"] == pytest.approx(0.0006)
    assert llm[("extract", "claude-sonnet-5-5")]["est_usd"] == pytest.approx(0.015)
    # A model with no price reports tokens and no cost, never a guess.
    assert llm[("ask", "claude-future-9")]["est_usd"] is None
    # Another tenant's 99999-token row and the 25-hour-old row are both absent.
    assert ("ask", "claude-haiku-5-5") not in llm


def test_stats_are_admin_only(client, staff, seeded):
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers=staff(Role.REVIEWER)).status_code == 403


def test_an_empty_tenant_is_all_zeros(client, staff, tenant):
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()
    assert body["documents_by_status"] == {}
    assert body["dead_letter"] == body["running_jobs"] == body["stale_running"] == 0
    assert body["oldest_due_pending_s"] == {}
    assert body["llm_24h"] == []


def test_the_price_table():
    assert est_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert est_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert est_usd("nope", 1, 1) is None
