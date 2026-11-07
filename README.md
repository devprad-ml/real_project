# Document ingestion prototype

Email a document to a client's intake address and it becomes searchable: staff can see
what arrived on the start page and ask questions about it in a chatbot with citations.

This is a prototype, one process with no job queue. The enterprise requirements are in
`Enterprise_Healthcare_Document_Management_BRD_with_Architecture.md`. `DESIGN.md`
records the larger production-shaped design this was simplified from; that code is in
git history, up to commit `e54e194`.

## What it does

```
IMAP mailbox  (polled every IMAP_POLL_SECONDS)
    v
intake      record bytes stored verbatim, sha256 dedupe per tenant
    v
pipeline    normalize / OCR -> classify (Claude) -> chunk + embed -> qdrant
    v
GET /       every document, who emailed it, when, and its state
GET /chat   questions answered from the documents, with citations
```

One email can produce zero, one or many documents. Forwarded mail is unwrapped, so the
PDF inside a forwarded message becomes the document.

**Multiple things at once.** Sync routes run on FastAPI's thread pool, and documents run
on their own pool of `INGEST_WORKERS` threads in the API process, so uploads process side
by side and never block a chat or search request.

**State** is three columns on `documents`:

| `processed_at` | `attempts` | Shown as |
| :--- | :--- | :--- |
| set | any | ready |
| null | under `MAX_ATTEMPTS` | processing, retried on the next timer tick |
| null | at `MAX_ATTEMPTS` | failed, with the full reason in `documents.error` |

An attempt is counted before the work starts, so a document that crashes the process
cannot crash it on every restart. To retry a failed document, set its `attempts` to 0.

**Limits of the prototype:** run one API process only (`--workers 1`). Two would process
the same documents twice. OCR shares CPU with the API, so keep `INGEST_WORKERS` near the
core count.

Kept from the larger design: tenant isolation (a qdrant collection per client),
API-key auth, the append-only `audit_log`, and record bytes that are never modified.

## Layout

```
src/app/
  api/          routers: dashboard, chat, ask, search, documents, admin, health
  ingestion/    IMAP poller, intake, pipeline, reindex
  rag/          normalize, pdftext, classify, chunk, embed, bm25, answer, llm
  search/       query text to ranked, audited chunks
  storage/      blob store and vector store (qdrant or Azure AI Search)
  models/       SQLAlchemy models
alembic/        migrations
bench/          load test and a messy test corpus
```

## Setup

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env          # then fill it in
docker compose up -d          # postgres on 5432, qdrant on 6333
uv sync
uv run alembic upgrade head   # destructive on an older database, see below
```

Upgrading a database from before this simplification drops `jobs`, `associations`,
`extracted_fields` and `members`, plus the `status`, `summary` and `route_queue` columns
on `documents`. There is no downgrade, so back it up first if you want that data.

OCR needs Tesseract and Ghostscript on the host (`choco install tesseract ghostscript`).
`unpaper` is optional and only adds despeckling. The embedding model downloads once and
runs locally.

`Settings` in `src/app/config.py` is the full configuration. Set `IMAP_HOST`,
`IMAP_USER` and `IMAP_PASSWORD` to poll a mailbox. A client's address is the
`clients.intake_address` column, matched against `Delivered-To`, `To` and `Cc`.

## Commands

```bash
uvicorn app.main:app --no-access-log      # the app, then open http://127.0.0.1:8000
python -m bench.seed tenant <address>     # a client and an API key to paste into the page
python -m app.keys --help                 # manage API keys
python -m app.ingestion.reindex           # rebuild vectors from stored PDFs
uv run pytest                             # needs Postgres; qdrant tests skip without it
```

`.env.test` points at the same `app_db` as development by default, and tests write to
that database.

## Not built

Compared with the BRD: field extraction, human review, member matching, routing and
downstream push were removed from this prototype, and there is no guardrail layer yet.

## Tracing and evals

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` to trace questions
and documents with their timing, tokens and cost. Question and document text is only sent
with `LANGFUSE_CAPTURE_CONTENT=true`, so use that for synthetic data or under a BAA.

```bash
python -m bench.rag_eval load                 # synthetic documents, prints an API key
python -m bench.rag_eval run <key> [base url] # 33 scored questions as a Langfuse experiment
```
