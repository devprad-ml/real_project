"""intake_bytes: the RECEIVED transition, with no mailbox involved."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind
from app.ingestion.intake import intake_bytes, safe_filename
from app.models import Client, Document, Job, Log


def _intake(tenant, data=b"a denial letter", filename="denial.pdf"):
    with SessionLocal() as s:
        doc = intake_bytes(
            s, tenant_id=tenant, data=data, filename=filename, source_id=None
        )
        s.commit()
        return doc


def _docs(tenant):
    with SessionLocal() as s:
        return s.scalars(select(Document).where(Document.tenant_id == tenant)).all()


def test_intake_writes_the_row_the_blob_and_the_job(tenant, blob):
    data = b"a denial letter"
    doc = _intake(tenant, data)
    sha = hashlib.sha256(data).hexdigest()

    # Record bytes, untouched, at a key addressed by tenant and content.
    assert doc.blob_key == f"raw/{tenant}/{sha}/denial.pdf"
    assert blob.data[doc.blob_key] == data

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.RECEIVED
        assert fresh.sha256 == sha
        assert fresh.source_filename == "denial.pdf"

        jobs = s.scalars(select(Job).where(Job.document_id == doc.id)).all()
        assert [j.kind for j in jobs] == [JobKind.NORMALIZE]

        logs = s.scalars(select(Log).where(Log.document_id == doc.id)).all()
        assert [log.detail["to"] for log in logs] == ["RECEIVED"]


def test_the_same_bytes_twice_is_one_document(tenant, blob):
    first = _intake(tenant)
    second = _intake(tenant, filename="forwarded-copy.pdf")

    assert second is None
    assert len(_docs(tenant)) == 1
    with SessionLocal() as s:
        # Still one job -- a duplicate must not re-run the pipeline.
        assert len(s.scalars(select(Job).where(Job.tenant_id == tenant)).all()) == 1
        logs = s.scalars(select(Log).where(Log.document_id == first.id)).all()
        assert [log.detail.get("event") for log in logs] == [None, "duplicate_intake"]


def test_the_same_bytes_for_two_tenants_is_two_documents(tenant, blob):
    """The dedupe key is (tenant_id, sha256). The same fax sent to two clients is two
    records, or one client could see another had received it."""
    other = uuid.uuid4()
    with SessionLocal() as s:
        s.add(Client(id=other, name=f"test-{other}"))
        s.commit()
    try:
        assert _intake(tenant) is not None
        assert _intake(other) is not None
    finally:
        with SessionLocal() as s:
            from sqlalchemy import text

            for table in ("jobs", "audit_log", "documents"):
                s.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                    {"t": other},
                )
            s.execute(text("DELETE FROM clients WHERE id = :t"), {"t": other})
            s.commit()


@pytest.mark.parametrize(
    "hostile, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\x.pdf", "x.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("evil\x00name.pdf", "evilname.pdf"),
        ("", "unnamed"),
        ("..", "unnamed"),
        # Windows drive-relative: Path("_blobs") / "raw/t/s/C:evil.pdf" resolves to
        # C:evil.pdf, so the bytes would land outside the blob root entirely.
        ("C:evil.pdf", "Cevil.pdf"),
        ("C:/Windows/evil.pdf", "evil.pdf"),
    ],
)
def test_a_hostile_filename_cannot_escape_the_blob_prefix(hostile, expected):
    assert safe_filename(hostile) == expected


def test_the_written_path_stays_under_the_blob_root(tenant, blob, tmp_path):
    """The sanitiser is a string check; this is the property it exists for."""
    from pathlib import Path

    from app.storage.blob import LocalBlobStore

    store = LocalBlobStore(str(tmp_path))
    root = tmp_path.resolve()
    for hostile in ("C:evil.pdf", "../../escape.pdf", "..\\..\\escape.pdf"):
        key = f"raw/{tenant}/{'0' * 64}/{safe_filename(hostile)}"
        store.put(key, b"x", "application/octet-stream")
        assert root in Path(store._path(key)).resolve().parents
