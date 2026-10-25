""" testing code"""

import os
from pathlib import Path

from dotenv import load_dotenv

env = Path(__file__).resolve().parent.parent / ".env.test"
load_dotenv(dotenv_path=env, override=True)

# The API lifespan would load the 90 MB embedder for every `client` fixture.
os.environ.setdefault("WARM_EMBEDDER", "false")

# import pytest now
import math  # noqa: E402
import types  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# import app factory
from app.db import SessionLocal  # noqa: E402
from app import keys, storage  # noqa: E402
from app.api import documents, health, validation  # noqa: E402
from app.enums import DocumentStatus, DocumentType, Role  # noqa: E402
from app.ingestion import email_poller, intake  # noqa: E402
from app.jobs import handlers  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Client, Document  # noqa: E402
from app.processing import classify, embed, extract, llm, normalize, pdftext  # noqa: E402
from app.search import retrieve  # noqa: E402
from app.processing import bm25  # noqa: E402
from app.storage.vectors import Hit, Point, point_id, tenant_collection  # noqa: E402


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


def intake_address(tenant_id) -> str:
    return f"intake+{tenant_id.hex}@example.com"


def _throwaway_tenant():
    """A client row, plus teardown of everything hung off it.

    Order matters: children before the parent, or the FKs refuse.
    """
    tenant_id = uuid.uuid4()
    with SessionLocal() as session:
        session.add(
            Client(
                id=tenant_id,
                name=f"test-{tenant_id}",
                intake_address=intake_address(tenant_id),
            )
        )
        session.commit()

    yield tenant_id

    with SessionLocal() as session:
        for table in ("api_keys", "role_grants"):
            session.execute(
                text(  # noqa: S608
                    f"DELETE FROM {table} WHERE principal_id IN "
                    "(SELECT id FROM principals WHERE tenant_id = :t)"
                ),
                {"t": tenant_id},
            )
        for table in (
            "jobs",
            "llm_usage",
            "audit_log",
            "extracted_fields",
            "associations",
            "documents",
            "sources",
            "principals",
            "members",
        ):
            session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": tenant_id},
            )
        session.execute(text("DELETE FROM clients WHERE id = :t"), {"t": tenant_id})
        session.commit()


@pytest.fixture
def tenant():
    yield from _throwaway_tenant()


@pytest.fixture
def other_tenant():
    """A second client, for tests that one tenant cannot reach another's data."""
    yield from _throwaway_tenant()


@pytest.fixture
def staff(tenant):
    """Factory: a staff member of `tenant` with these roles. Returns the request
    headers carrying their new API key. Teardown is the tenant fixture's."""

    def _make(*roles: Role, tenant_id=None):
        _, key = keys.create(tenant_id or tenant, "test staff", list(roles))
        return {"Authorization": f"Bearer {key}"}

    return _make


@pytest.fixture
def make_doc(tenant):
    """Factory for documents rows at an arbitrary status. Intake tests go through
    intake_bytes instead; the pipeline tests need to start mid-pipeline."""

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

    def delete(self, key):
        self.data.pop(key, None)

    def url(self, key):
        return f"memory://{key}"


@pytest.fixture
def blob(monkeypatch):
    store = FakeBlobStore({"raw/test/file.pdf": b"original record bytes"})
    monkeypatch.setattr(handlers, "get_blob_store", lambda: store)
    monkeypatch.setattr(intake, "get_blob_store", lambda: store)
    monkeypatch.setattr(storage, "get_blob_store", lambda: store)
    monkeypatch.setattr(documents, "get_blob_store", lambda: store)
    monkeypatch.setattr(validation, "get_blob_store", lambda: store)
    return store


@pytest.fixture
def fake_tools(monkeypatch):
    """Stand in for Tesseract/Ghostscript. These tests are about the state machine,
    not about whether OCR can read a fax."""
    monkeypatch.setattr(normalize, "run", lambda data: (FAKE_PDFA, len(PAGE_TEXTS)))
    monkeypatch.setattr(pdftext, "pages", lambda pdf: enumerate(PAGE_TEXTS, start=1))


class FakeVectorStore:
    """In-memory stand-in for qdrant. Keyed by collection, then by point id, so an
    upsert of an id already present overwrites -- the one behaviour of a real vector
    DB that the embed handler's retry story depends on."""

    def __init__(self):
        self.collections: dict[str, dict[str, object]] = {}
        self.upserts = 0  # a replay that writes nothing still must not call upsert
        self.down = False  # ping() raises, as an unreachable qdrant would

    def ping(self):
        if self.down:
            raise ConnectionError("vector store down")

    def upsert(self, collection, points):
        self.upserts += 1
        target = self.collections.setdefault(collection, {})
        for p in points:
            target[p.id] = p

    def search(self, collection, vector, sparse, limit):
        """Hybrid like the real store: rank by cosine, rank by sparse dot product
        (points with no shared term sit out that leg), fuse with RRF. A missing
        collection is an empty result, same as the real store."""

        def cos(a, b):
            norm = math.hypot(*a) * math.hypot(*b)
            return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0

        def dot(query, doc):
            weights = dict(zip(*doc))
            return sum(v * weights.get(i, 0.0) for i, v in zip(*query))

        points = self.points(collection)
        dense = sorted(points, key=lambda p: cos(vector, p.vector), reverse=True)
        matched = [p for p in points if dot(sparse, p.sparse) > 0]
        keyword = sorted(matched, key=lambda p: dot(sparse, p.sparse), reverse=True)

        scores: dict[str, float] = {}
        for leg in (dense, keyword):
            for rank, p in enumerate(leg[: limit * 4]):
                scores[p.id] = scores.get(p.id, 0.0) + 1 / (60 + rank + 1)
        by_id = {p.id: p for p in points}
        hits = [Hit(pid, score, by_id[pid].payload) for pid, score in scores.items()]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]

    def drop(self, collection):
        self.collections.pop(collection, None)

    def points(self, collection):
        return list(self.collections.get(collection, {}).values())


@pytest.fixture
def vectors(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(handlers, "get_vector_store", lambda: store)
    monkeypatch.setattr(retrieve, "get_vector_store", lambda: store)
    monkeypatch.setattr(health, "get_vector_store", lambda: store)
    return store


# --- read path: chunks planted straight into a tenant's collection -------------

QUERY_VECTOR = [1.0, 0.0, 0.0]


@pytest.fixture
def query_encoder(monkeypatch):
    monkeypatch.setattr(embed, "encode", lambda texts: [QUERY_VECTOR for _ in texts])


@pytest.fixture
def plant(vectors, tenant):
    """Put a chunk straight into the tenant's collection, as handle_embed would."""

    def _plant(document_id, vector, *, page_no=1, idx=0, text="chunk"):
        vectors.upsert(
            tenant_collection(tenant),
            [
                Point(
                    id=point_id(document_id, page_no, idx),
                    vector=vector,
                    sparse=bm25.encode_doc(text),
                    payload={
                        "tenant_id": str(tenant),
                        "document_id": str(document_id),
                        "page_no": page_no,
                        "text": text,
                    },
                )
            ],
        )

    return _plant


class FakeMessages:
    """`client.messages` with only `parse`. Records every call's kwargs."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def fake_api(monkeypatch):
    """Replaces the shared Anthropic client. No test calls the real API."""

    def _install(response=None, error=None):
        messages = FakeMessages(response, error)
        fake = types.SimpleNamespace(messages=messages)
        monkeypatch.setattr(llm, "client", lambda: fake)
        return messages

    return _install


@pytest.fixture
def fake_classifier(monkeypatch):
    """No Anthropic call in any test. Records what it was shown and answers with
    whatever `result` holds, so a test can choose the type."""

    class Fake:
        result = classify.ClassifyResult(doc_type=DocumentType.CLAIMS, confidence=0.93)
        seen: list[str] = []

        def run(self, pages):
            self.seen.append("\n".join(pages))
            return self.result

    fake = Fake()
    fake.seen = []
    monkeypatch.setattr(classify, "run", fake.run)
    return fake


@pytest.fixture
def fake_extractor(monkeypatch):
    """No Anthropic call in any test. Records the doc_type it was shown and answers
    with whatever `result` holds, so a test can choose the fields and summary."""

    class Fake:
        result = extract.ExtractionResult(fields=[], summary="")
        seen: list[tuple] = []

        def run(self, doc_type, pages):
            self.seen.append((doc_type, "\n".join(pages)))
            return self.result

    fake = Fake()
    fake.seen = []
    monkeypatch.setattr(extract, "run", fake.run)
    return fake


@pytest.fixture
def fake_embedder(monkeypatch):
    """No torch, no model download in CI. The vector values are arbitrary but
    distinct per text, which is enough to assert chunk-to-vector alignment."""
    monkeypatch.setattr(
        embed, "encode", lambda texts: [[float(len(t)), 0.0, 1.0] for t in texts]
    )


# --- a fake IMAP mailbox ---------------------------------------------------------


class FakeMessage:
    """The slice of imap_tools.MailMessage the poller reads.

    `headers` mirrors imap_tools' LazyHeaders: keys lowercased, value a tuple of
    *every* occurrence. A forwarding chain really does carry several Delivered-To
    headers, and a dict that kept only the last would hide that from the tests.
    """

    def __init__(self, uid, obj):
        self.uid = None if uid is None else str(uid)
        self.obj = obj
        self.from_ = obj["From"]
        self.subject = obj["Subject"]
        self.headers = {
            key.lower(): tuple(v for k, v in obj.items() if k.lower() == key.lower())
            for key in obj.keys()
        }


def make_message(uid, *, to=None, attachments=(), extra_headers=()):
    """attachments: (filename, bytes) pairs, built into a real MIME message.
    extra_headers: pairs, not a dict -- repeated headers are the point."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "billing@hospital.example"
    if to is not None:
        msg["To"] = to
    msg["Subject"] = f"documents {uid}"
    pairs = getattr(extra_headers, "items", lambda: extra_headers)()
    for k, v in pairs:
        msg[k] = v
    msg.set_content("see attached")
    for filename, data in attachments:
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=filename
        )
    return FakeMessage(uid, msg)


class FakeMailBox:
    """In-memory imap_tools.MailBox: the login context manager, fetch, flag, and the
    UIDVALIDITY lookup -- the only surface the poller touches."""

    def __init__(self):
        self.messages = []
        self.seen = set()
        self.flag_fails = False  # a flag that never lands, after the commit did

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def folder(self):
        return self

    def status(self, name):
        return {"UIDVALIDITY": 1}

    def fetch(self, criteria, **kwargs):
        return [m for m in self.messages if m.uid not in self.seen]

    def flag(self, uid, flag, value):
        if self.flag_fails:
            raise ConnectionError("imap connection dropped")
        self.seen.add(uid)


@pytest.fixture
def mailbox(monkeypatch):
    box = FakeMailBox()
    monkeypatch.setattr(email_poller, "_open_mailbox", lambda: box)
    return box


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
