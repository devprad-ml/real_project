"""GET /: the start page, and the rows behind it."""

import datetime
import uuid

from app.config import get_settings
from app.db import SessionLocal
from app.enums import Role
from app.models import Src


def _email(tenant) -> uuid.UUID:
    source_id = uuid.uuid4()
    with SessionLocal() as s:
        s.add(
            Src(
                id=source_id,
                tenant_id=tenant,
                ref=f"box:1:{source_id}",
                channel="email",
                raw_meta={
                    "from": "billing@hospital.example",
                    "subject": "Claim C-77",
                    "sent_at": "2026-11-05T09:30:00+00:00",
                },
            )
        )
        s.commit()
    return source_id


def test_the_start_page_is_served_without_a_key(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Ingested documents" in r.text


def test_a_row_carries_the_email_it_came_from(client, staff, tenant, make_doc):
    doc = make_doc(source_id=_email(tenant), source_filename="claim.pdf", attempts=1)

    [row] = client.get("/intake", headers=staff(Role.REVIEWER)).json()

    assert row["id"] == str(doc.id)
    assert row["filename"] == "claim.pdf"
    assert row["sender"] == "billing@hospital.example"
    assert row["subject"] == "Claim C-77"
    assert row["sent_at"] == "2026-11-05T09:30:00+00:00"
    assert row["state"] == "processing"


def test_state_follows_the_document(client, staff, make_doc):
    make_doc(source_filename="ok.pdf", processed_at=datetime.datetime(2026, 11, 1))
    make_doc(
        source_filename="bad.pdf",
        attempts=get_settings().max_attempts,
        error="Jane Roe unreadable",
    )

    r = client.get("/intake", headers=staff(Role.REVIEWER))

    states = {row["filename"]: row["state"] for row in r.json()}
    assert states == {"ok.pdf": "ready", "bad.pdf": "failed"}
    assert "Jane Roe" not in r.text


def test_a_document_with_no_email_still_lists(client, staff, make_doc):
    make_doc(source_filename="loose.pdf")

    [row] = client.get("/intake", headers=staff(Role.REVIEWER)).json()

    assert (row["sender"], row["subject"], row["sent_at"]) == (None, None, None)


def test_rows_are_scoped_to_the_callers_tenant(
    client, staff, make_doc, other_tenant
):
    make_doc(source_filename="mine.pdf")

    r = client.get("/intake", headers=staff(Role.REVIEWER, tenant_id=other_tenant))

    assert r.json() == []


def test_the_rows_need_a_key(client, tenant):
    assert client.get("/intake").status_code == 401
