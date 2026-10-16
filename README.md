# Document ingestion pipeline

Multi-tenant intake and RAG indexing for healthcare documents. An email lands in a
mailbox, and the platform turns it into searchable, citable, per-tenant vectors with an
audit trail behind every transition.

Built as a learning project from `DESIGN.md`, which derives from
`Enterprise_Healthcare_Document_Management_BRD_with_Architecture.md`.
`NORMALIZATION.md` is the build spec for the current phase (email intake, normalize,
OCR, embed).

## What it does

```
IMAP mailbox
    |  poll_once()  (a tick in the worker loop)
    v
[*] --> RECEIVED     intake: record bytes stored verbatim, sha256 dedupe, normalize job queued
    --> NORMALIZED   derived searchable PDF/A written next to the record, page count
    --> OCR_DONE     per-page text rows in Postgres
    --> EMBEDDED     text chunked, embedded locally, upserted to a per-tenant qdrant collection
    --> FAILED       terminal, after MAX_ATTEMPTS, with an audit row
```

One email can produce zero, one, or many documents: one accepted attachment is one
`documents` row. Forwarded mail is unwrapped, so the PDF inside a `message/rfc822` part
becomes the document rather than an unreadable `.eml`.

`GET /search?q=...` with an `X-Tenant-Id` header is the read side: the query is
embedded with the same model as the chunks, searched in that tenant's collection, and
each hit comes back as a citation (document, filename, page, matching text). Every
document a search returns gets a `VIEW` row in `audit_log`.

Design rules that the code actually enforces:

- **Record vs derived.** The bytes that arrived are never modified. Everything a job
  computes is a derived artifact under a separate blob key and can be deleted and
  rebuilt. `sha256` is always taken over the record, never over a derived file.
- **One job is one transition.** Every handler guards on `documents.status` and is a
  no-op if the document is already past that state, so a retry is safe.
- **At least once, never at most once.** The poller marks a message `\Seen` only after
  the database transaction commits. A crash costs a re-fetch, not a lost document.
- **Tenant isolation is physical where it matters.** Vectors live in
  `chunks_{tenant_id}`, a separate qdrant collection per client, so a forgotten filter
  cannot serve the wrong tenant's chart notes. A wrong collection name fails closed.
- **`audit_log` is append only.** A migration revokes UPDATE and DELETE from the app
  role, and `tests/test_privileges.py` asserts it.
- **Two processes, one Postgres.** The API only writes rows and returns. The worker does
  every slow thing: OCR, embedding, polling.

## Layout

```
src/app/
  api/          FastAPI routers: health, search
  ingestion/    intake_bytes() and the IMAP poller
  jobs/         the Postgres job queue, the worker loop, the handler table
  processing/   normalize, ocr, chunk, embed
  search/       retrieve: query text to ranked, audited chunks
  storage/      BlobStore and VectorStore protocols plus local/qdrant implementations
  models/       SQLAlchemy 2.0 models
  audit/        write_audit()
  config.py     pydantic-settings Settings, cached
  db.py         engine, SessionLocal, get_session
alembic/        migrations, including the role grants and the audit revoke
tests/          pytest, one real Postgres, faked qdrant and IMAP
```

Dependency direction is one way: `api`, `ingestion` and `jobs.worker` are entrypoints,
they call into `processing`, `storage` and `audit`, and those call `models`, `db` and
`config`. Nothing lower imports anything higher.

The job queue is a Postgres `jobs` table claimed with
`SELECT ... FOR UPDATE SKIP LOCKED`. Run more worker copies to scale: no coordination
between them is needed. Celery or Redis is the documented upgrade path, not the start.

## Setup

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), and Docker for Postgres and
qdrant.

```bash
cp .env.example .env          # then fill it in, see below
docker compose up -d          # postgres on 5432, qdrant on 6333
uv sync                       # installs the package editable, required before tests pass
uv run alembic upgrade head   # creates tables, the app_user role and its grants
```

`normalize` and `ocr` shell out to the OCR toolchain through `ocrmypdf`, which is not
pure Python. There is no worker container yet (see `NORMALIZATION.md` 2.5), so install
the binaries on the host:

```
choco install tesseract ghostscript poppler                        # Windows
apt install tesseract-ocr ghostscript poppler-utils unpaper qpdf   # Debian
```

Everything else is free and local. The embedding model (`all-MiniLM-L6-v2` by default)
downloads its weights once and runs on CPU, so there is no API key and no per-call cost
on the embed path.

### Environment

`Settings` in `src/app/config.py` is the full list. The ones without a default have to
be set:

| Variable | Why |
| :--- | :--- |
| `DATABASE_URL` | the narrow `app_user` role the API and worker run as |
| `ALEMBIC_DATABASE_URL` | the owner role, the only one that can CREATE TABLE and GRANT. Leave unset where `DATABASE_URL` already has those rights, as in CI |
| `ANTHROPIC_API_KEY` | unused on the current pipeline, still required by `Settings` |
| `DOWNSTREAM_BASE_URL` | the same, reserved for the push milestone |

Worth knowing about the optional ones: `IMAP_HOST` unset keeps the poller tick switched
off, which is how tests and CI keep the worker loop offline. `EMBED_MODEL` and
`EMBED_DIM` must agree, because a qdrant collection is created with a fixed vector
width, so changing the model means a new collection and a re-embed from
`document_pages`. That is why OCR text is kept in Postgres.

Email routing comes from the database, not from config: `clients.intake_address` holds
a plus-address such as `intake+acme@example.com`, matched case insensitively against
`Delivered-To`, then `To` and `Cc`. One mailbox, one credential set, N tenants, and a
new client is a row rather than a redeploy.

## Commands

```bash
uvicorn app.main:app --reload       # the API process
python -m app.jobs.worker           # the worker: claim, dispatch, commit, forever
uv run pytest                       # the suite
uv run pytest tests/test_intake.py  # one file
uv run alembic downgrade -1         # migrations round trip, both directions
```

## Tests

The suite needs a real Postgres, and `DATABASE_URL` plus the other required settings in
the environment. The embedding model and IMAP are faked, and so is qdrant everywhere
except `tests/test_qdrant.py`, which skips when no qdrant answers. Torch is never
loaded:

- `FakeVectorStore` keys points by id, which is what makes the overwrite-on-retry
  behaviour testable.
- `FakeMailBox` mirrors only the `imap-tools` surface the poller uses, and returns
  repeated headers as a tuple the way the real client does, because a forwarding chain
  genuinely carries several `Delivered-To` headers.
- `APP_USER_DATABASE_URL` points at the restricted role. Without it the privilege tests
  skip and the grant model goes untested, since CI is otherwise a superuser.

Coverage is not the goal. Each test should fail for exactly one real reason.

## Status

Email intake through to embedded vectors works end to end. The milestone table in
`DESIGN.md` 11 is stale: it predates the pivot to email-only intake and vector search,
so the per-milestone notes in `.claude/context/` are the accurate record of what was
built and why.

Known gaps, roughly in the order they matter:

- **No authentication.** `/search` takes the tenant from a trusted `X-Tenant-Id`
  header, so anyone who can reach the API can read any tenant. Localhost only.
- No answer generation over the search hits yet, and retrieval quality has not been
  measured with the real embedding model.
- Nothing has run against a real IMAP server. `tests/test_qdrant.py` runs against a
  real qdrant when one answers, and skips otherwise.
- No worker container, so the OCR toolchain has to be installed on the host.
- Unroutable email is dropped with a log line. A real deployment wants a quarantine
  mailbox.
- Byte cap at intake only. A page-count cap belongs in `normalize`, where the page
  count is first known.
- Classification, extraction, human review, association, routing and downstream push
  are deferred, not dropped. Postgres full-text search alongside the vector index is
  likewise deferred: both are required end states per the BRD.

Deliberate shortcuts are marked with a `ponytail:` comment in the code naming the
ceiling and the upgrade path.
