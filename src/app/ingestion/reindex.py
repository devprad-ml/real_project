''' Rebuild vector collections from blob storage.

    python -m app.ingestion.reindex                 # every tenant
    python -m app.ingestion.reindex --tenant <uuid>  # one tenant

The vector store is an index over the normalized PDFs, never the only copy of
anything, so it can be dropped and rebuilt at any time: after a schema change (new
vector, new model), a corrupted collection, or a restore. Nothing
is audited, because no document changes.

Drops first, then refills, so that tenant's search returns fewer results until the
run finishes.
ponytail: blackout per tenant during the rebuild. Zero-downtime version: build into a
second collection and flip a qdrant alias.
'''

import argparse
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.ingestion.pipeline import index_document
from app.models import Client, Document
from app.storage import get_vector_store
from app.storage.vectors import tenant_collection


def reindex_tenant(session: Session, tenant_id: uuid.UUID) -> tuple[int, int]:
    ''' Returns (documents, chunks) written. '''
    get_vector_store().drop(tenant_collection(tenant_id))
    docs = session.scalars(
        select(Document)
        .where(Document.tenant_id == tenant_id, Document.processed_at.is_not(None))
        .order_by(Document.received_at)
    ).all()
    chunks = sum(index_document(doc) for doc in docs)
    return len(docs), chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=uuid.UUID, help="only this tenant")
    args = parser.parse_args()

    with SessionLocal() as session:
        tenants = (
            [args.tenant] if args.tenant else session.scalars(select(Client.id)).all()
        )
        for tenant_id in tenants:
            docs, chunks = reindex_tenant(session, tenant_id)
            print(f"{tenant_id}: {docs} document(s), {chunks} chunk(s)")


if __name__ == "__main__":
    main()
