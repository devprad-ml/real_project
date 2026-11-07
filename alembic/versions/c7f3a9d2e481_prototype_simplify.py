"""Prototype: no job queue or status machine, no review, MPI, routing or push

Revision ID: c7f3a9d2e481
Revises: b4d9f1a7c260
Create Date: 2026-11-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7f3a9d2e481'
down_revision: Union[str, Sequence[str], None] = 'b4d9f1a7c260'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('processed_at', sa.DateTime(), nullable=True))
    op.add_column(
        'documents',
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column('documents', sa.Column('error', sa.Text(), nullable=True))

    # Anything already embedded is searchable, so it counts as processed. That includes
    # a document that failed at a step after embed.
    op.execute(
        """
        UPDATE documents d SET processed_at = d.updated_at
        WHERE d.status NOT IN ('RECEIVED', 'NORMALIZED', 'CLASSIFIED', 'FAILED')
           OR EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.document_id = d.id AND j.state = 'ERROR'
                  AND j.kind IN ('EXTRACT', 'ASSOCIATE', 'ROUTE', 'PUSH'))
        """
    )
    # Failed before embed: keep the reason, and do not retry on the next startup.
    op.execute(
        """
        UPDATE documents d SET attempts = 5, error = (
            SELECT j.last_error FROM jobs j
            WHERE j.document_id = d.id AND j.state = 'ERROR'
            ORDER BY j.created_at DESC LIMIT 1)
        WHERE d.status = 'FAILED' AND d.processed_at IS NULL
        """
    )

    op.drop_column('documents', 'status')
    op.drop_column('documents', 'route_queue')
    op.drop_column('documents', 'summary')
    for table in ('jobs', 'associations', 'extracted_fields', 'members'):
        op.drop_table(table)


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError(
        "The dropped tables' rows are gone: restore a database backup instead."
    )
