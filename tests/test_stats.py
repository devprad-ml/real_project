"""GET /admin/stats: documents and Claude spend for one tenant."""

import datetime
import uuid

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentType, Role
from app.models import Document
from app.models.llm_usage import LlmUsage
from app.observability.stats import est_usd


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


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
    make_doc(processed_at=_utcnow())
    make_doc(processed_at=_utcnow())
    make_doc(attempts=1)
    make_doc(attempts=get_settings().max_attempts, error="boom")
    with SessionLocal() as s:
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
            doc_type=DocumentType.UNKNOWN, blob_key="raw/x",
        )
        s.add(other_doc)
        s.add(_usage(other_tenant, "ask", "claude-haiku-5-5", 99999, 99999, 99999))
        s.commit()


def test_the_numbers_for_a_known_tenant(client, staff, seeded, vectors):
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()

    assert body["documents"] == {"ready": 2, "processing": 1, "failed": 1}


def test_llm_spend_by_purpose_and_model_over_24_hours(client, staff, seeded, vectors):
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


def test_stats_report_the_vector_store_readiness_does_not_check(
    client, staff, tenant, vectors
):
    assert client.get("/admin/stats", headers=staff(Role.ADMIN)).json()[
        "vector_store"] == "ok"
    vectors.down = True
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()
    assert body["vector_store"] == "unreachable"


def test_stats_are_admin_only(client, staff, seeded):
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers=staff(Role.REVIEWER)).status_code == 403


def test_an_empty_tenant_is_all_zeros(client, staff, tenant, vectors):
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()
    assert body["documents"] == {"ready": 0, "processing": 0, "failed": 0}
    assert body["llm_24h"] == []


def test_the_price_table():
    assert est_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert est_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert est_usd("nope", 1, 1) is None
