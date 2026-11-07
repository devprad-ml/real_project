"""GET /admin/stats: document counts for one tenant."""

import datetime
import uuid

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentType, Role
from app.models import Document


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def seeded(tenant, other_tenant, make_doc):
    """A tenant with a known mix, and another tenant whose rows must not show up."""
    make_doc(processed_at=_utcnow())
    make_doc(processed_at=_utcnow())
    make_doc(attempts=1)
    make_doc(attempts=get_settings().max_attempts, error="boom")
    with SessionLocal() as s:
        # Another tenant's data, which must not appear.
        other_doc = Document(
            tenant_id=other_tenant, sha256=uuid.uuid4().hex * 2,
            doc_type=DocumentType.UNKNOWN, blob_key="raw/x",
        )
        s.add(other_doc)
        s.commit()


def test_the_numbers_for_a_known_tenant(client, staff, seeded, vectors):
    body = client.get("/admin/stats", headers=staff(Role.ADMIN)).json()

    assert body["documents"] == {"ready": 2, "processing": 1, "failed": 1}


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
