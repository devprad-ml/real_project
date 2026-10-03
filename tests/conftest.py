""" testing code"""

from pathlib import Path

from dotenv import load_dotenv

env = Path(__file__).resolve().parent.parent / ".env.test"
load_dotenv(dotenv_path=env, override=True)

# import pytest now
import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

# import app factory
from app.db import SessionLocal  # noqa: E402
from app.enums import DocumentStatus, DocumentType  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Client, Document  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    """A plain session. Tests that exercise the queue must commit for real --
    SKIP LOCKED is only observable across committed transactions."""
    with SessionLocal() as session:
        yield session


@pytest.fixture
def tenant():
    """A throwaway client row, plus teardown of everything hung off it.

    Order matters: children before the parent, or the FKs refuse.
    """
    tenant_id = uuid.uuid4()
    with SessionLocal() as session:
        session.add(Client(id=tenant_id, name=f"test-{tenant_id}"))
        session.commit()

    yield tenant_id

    with SessionLocal() as session:
        for table in ("jobs", "document_pages", "audit_log", "sources", "documents"):
            session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": tenant_id},
            )
        session.execute(text("DELETE FROM clients WHERE id = :t"), {"t": tenant_id})
        session.commit()


@pytest.fixture
def make_doc(tenant):
    """Factory for documents rows. No intake channel exists yet, so tests seed
    directly."""

    def _make(status=DocumentStatus.RECEIVED, **kwargs):
        with SessionLocal() as session:
            doc = Document(
                tenant_id=tenant,
                sha256=kwargs.pop("sha256", uuid.uuid4().hex * 2),
                doc_type=DocumentType.UNKNOWN,
                blob_key=kwargs.pop("blob_key", "raw/test/file.pdf"),
                status=status,
                **kwargs,
            )
            session.add(doc)
            session.commit()
            return doc

    return _make
