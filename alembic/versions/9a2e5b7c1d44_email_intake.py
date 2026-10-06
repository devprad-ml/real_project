"""email intake: clients.intake_address, documents.source_id/source_filename

Revision ID: 9a2e5b7c1d44
Revises: 7c1d4e2fa903
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a2e5b7c1d44'
down_revision: Union[str, Sequence[str], None] = '7c1d4e2fa903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('clients', sa.Column('intake_address', sa.String(length=255), nullable=True))
    op.create_unique_constraint('unique_intake_address', 'clients', ['intake_address'])
    op.add_column('documents', sa.Column('source_id', sa.Uuid(), nullable=True))
    op.add_column('documents', sa.Column('source_filename', sa.String(length=255), nullable=True))
    op.create_foreign_key('fk_documents_source_id', 'documents', 'sources', ['source_id'], ['id'])
    # No grant changes: app_user already has SELECT on clients and SELECT/INSERT on
    # documents and sources, and intake never updates a sources row.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_documents_source_id', 'documents', type_='foreignkey')
    op.drop_column('documents', 'source_filename')
    op.drop_column('documents', 'source_id')
    op.drop_constraint('unique_intake_address', 'clients', type_='unique')
    op.drop_column('clients', 'intake_address')
