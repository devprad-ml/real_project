""" testing code"""

from pathlib import Path

from dotenv import load_dotenv

env = Path(__file__).resolve().parent.parent / ".env.test"
load_dotenv(dotenv_path=env, override=True)

# import pytest now
import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# import app factory
from app.db import SessionLocal  # noqa: E402
from app.enums import DocumentStatus, DocumentType  # noqa: E402
from app.jobs import handlers  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Client, Document  # noqa: E402
from app.processing import embed, normalize, ocr  # noqa: E402


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


# --- fakes shared by the handler and worker tests ----------------------------

FAKE_PDFA = b"%PDF-1.7 fake pdfa"
PAGE_TEXTS = ["page one text", "page two text", ""]


class FakeBlobStore:
    """In-memory BlobStore. Structurally compatible -- Protocol, not inheritance."""

    def __init__(self, seed=None):
        self.data = dict(seed or {})

    def put(self, key, data, content_type):
        self.data[key] = data

    def get(self, key):
        return self.data[key]

    def url(self, key):
        return f"memory://{key}"


@pytest.fixture
def blob(monkeypatch):
    store = FakeBlobStore({"raw/test/file.pdf": b"original record bytes"})
    monkeypatch.setattr(handlers, "get_blob_store", lambda: store)
    return store


@pytest.fixture
def fake_tools(monkeypatch):
    """Stand in for Tesseract/Ghostscript. These tests are about the state machine,
    not about whether OCR can read a fax."""
    monkeypatch.setattr(normalize, "run", lambda data: (FAKE_PDFA, len(PAGE_TEXTS)))
    monkeypatch.setattr(ocr, "pages", lambda pdf: enumerate(PAGE_TEXTS, start=1))


class FakeVectorStore:
    """In-memory stand-in for qdrant. Keyed by collection, then by point id, so an
    upsert of an id already present overwrites -- the one behaviour of a real vector
    DB that the embed handler's retry story depends on."""

    def __init__(self):
        self.collections: dict[str, dict[str, object]] = {}
        self.upserts = 0  # a replay that writes nothing still must not call upsert

    def upsert(self, collection, points):
        self.upserts += 1
        target = self.collections.setdefault(collection, {})
        for p in points:
            target[p.id] = p

    def drop(self, collection):
        self.collections.pop(collection, None)

    def points(self, collection):
        return list(self.collections.get(collection, {}).values())


@pytest.fixture
def vectors(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(handlers, "get_vector_store", lambda: store)
    return store


@pytest.fixture
def fake_embedder(monkeypatch):
    """No torch, no model download in CI. The vector values are arbitrary but
    distinct per text, which is enough to assert chunk-to-vector alignment."""
    monkeypatch.setattr(
        embed, "encode", lambda texts: [[float(len(t)), 0.0, 1.0] for t in texts]
    )


# --- the restricted application role -----------------------------------------

APP_USER_URL = os.environ.get("APP_USER_DATABASE_URL")


@pytest.fixture(scope="session")
def app_user_sessionmaker():
    """Sessions bound to `app_user`, the role the API and worker actually run as.

    The rest of the suite connects as the owner because teardown needs DELETE. That
    means no other test can see the grant model at all -- an handler that deleted a
    row would pass here and raise InsufficientPrivilege in the worker.
    """
    if not APP_USER_URL:
        pytest.skip("APP_USER_DATABASE_URL not set; privilege tests need app_user")
    engine = create_engine(APP_USER_URL, pool_pre_ping=True)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def app_user_session(app_user_sessionmaker):
    with app_user_sessionmaker() as session:
        yield session
        session.rollback()
