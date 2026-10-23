"""M7: extracted_fields table

Revision ID: f4e2c589ae84
Revises: b6e1f0c3a812
Create Date: 2026-10-17

Documents already EMBEDDED when this ran get their extract jobs from 7ab04fb52bf7.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4e2c589ae84'
down_revision: Union[str, Sequence[str], None] = 'b6e1f0c3a812'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'extracted_fields',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column(
            'human_verified', sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # UPDATE now, not just SELECT/INSERT -- M8's review flow sets human_verified on
    # this same table, and there's no reason to churn the grant twice.
    op.execute("GRANT SELECT, INSERT, UPDATE ON extracted_fields TO app_user;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE SELECT, INSERT, UPDATE ON extracted_fields FROM app_user;")
    op.drop_table('extracted_fields')
