"""M6: classify step, drop document_pages and the ocr job

Revision ID: b6e1f0c3a812
Revises: 9a2e5b7c1d44
Create Date: 2026-10-16

Run with the workers stopped: a worker mid-job could commit an OCR_DONE status or an
ocr job after this has rewritten them.

The text now lives only in the normalized PDF/A, and the pipeline gains `classify`
between normalize and embed. Every existing document is unclassified, so the simplest
correct move is to send all of them back through the new pipeline from NORMALIZED.
Afterwards, run `python -m app.jobs.reindex` once to drop the old single-vector
collections; the workers refill them as documents pass through embed.

Status and job kind columns are `native_enum=False` (varchar holding the member NAME,
no CHECK), so removing OCR_DONE and OCR is a data change here, not DDL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6e1f0c3a812'
down_revision: Union[str, Sequence[str], None] = '9a2e5b7c1d44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Audit first, while the old status is still readable: every state change has
    #    an audit row, including this one.
    op.execute("""
        INSERT INTO audit_log (id, tenant_id, document_id, actor, action, detail, at)
        SELECT gen_random_uuid(), tenant_id, id, 'migration', 'STATE_CHANGE',
               jsonb_build_object('from', status, 'to', 'NORMALIZED',
                                  'reason', 'M6: re-run through classify'),
               now()
        FROM documents
        WHERE status IN ('OCR_DONE', 'EMBEDDED')
    """)
    op.execute("""
        UPDATE documents SET status = 'NORMALIZED'
        WHERE status IN ('OCR_DONE', 'EMBEDDED')
    """)

    # 2. Jobs. Every ocr row goes: a leftover 'OCR' would fail to load once the enum
    #    member is gone, and audit_log already holds that history. Open embed jobs
    #    belonged to OCR_DONE documents, which now need classify first. ERROR rows
    #    for other kinds stay: they explain why a FAILED document failed.
    op.execute("DELETE FROM jobs WHERE kind = 'OCR'")
    op.execute(
        "DELETE FROM jobs WHERE kind = 'EMBED' AND state IN ('PENDING', 'RUNNING')"
    )
    # Every NORMALIZED document now gets exactly one pending classify job: the ones
    # rewound above, and the ones that were waiting on the deleted ocr job.
    op.execute("""
        INSERT INTO jobs (id, tenant_id, document_id, kind, state, attempts,
                          run_after, created_at)
        SELECT gen_random_uuid(), tenant_id, id, 'CLASSIFY', 'PENDING', 0, now(), now()
        FROM documents
        WHERE status = 'NORMALIZED'
    """)

    # 3. The text is in the PDF/A. Dropping the table drops app_user's grants on it.
    op.drop_table('document_pages')


def downgrade() -> None:
    """Downgrade schema.

    Recreates an empty document_pages with its grant. Data is not reversed: the page
    text is re-derivable from the PDF/As, and documents already past CLASSIFIED cannot
    be mapped back onto the old pipeline's statuses.
    """
    op.create_table(
        'document_pages',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('page_no', sa.Integer(), nullable=False),
        sa.Column('ocr_text', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'document_id', 'page_no', name='doc_pages_unique_constraints'
        ),
    )
    op.execute("GRANT SELECT, INSERT ON document_pages TO app_user")
