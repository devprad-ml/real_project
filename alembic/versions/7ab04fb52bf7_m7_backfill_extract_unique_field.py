"""M7 follow-up: backfill extract jobs, one row per field per document

Revision ID: 7ab04fb52bf7
Revises: f4e2c589ae84
Create Date: 2026-10-17

Only handle_embed enqueues extract, and it is a no-op past CLASSIFIED -- so a
document that was already EMBEDDED before M7 would never be extracted. Give each
one a pending extract job, skipping any that already got one from the new code.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7ab04fb52bf7'
down_revision: Union[str, Sequence[str], None] = 'f4e2c589ae84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        INSERT INTO jobs (id, tenant_id, document_id, kind, state, attempts,
                          run_after, created_at)
        SELECT gen_random_uuid(), d.tenant_id, d.id, 'EXTRACT', 'PENDING', 0,
               now(), now()
        FROM documents d
        WHERE d.status = 'EMBEDDED'
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.document_id = d.id AND j.kind = 'EXTRACT'
                AND j.state IN ('PENDING', 'RUNNING')
          )
    """)
    op.create_unique_constraint(
        'unique_document_field', 'extracted_fields', ['document_id', 'name']
    )


def downgrade() -> None:
    """Downgrade schema. Backfilled jobs stay: jobs are history, and the extract
    handler still exists at f4e2c589ae84."""
    op.drop_constraint('unique_document_field', 'extracted_fields', type_='unique')
