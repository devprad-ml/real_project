"""M13: llm_usage ledger

Revision ID: a2c8e5d1b937
Revises: e1b7c3d9a524
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2c8e5d1b937'
down_revision: Union[str, Sequence[str], None] = 'e1b7c3d9a524'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'llm_usage',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=True),
        sa.Column('purpose', sa.String(length=12), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_llm_usage_tenant_at', 'llm_usage', ['tenant_id', 'created_at'])
    # Insert and read only, as audit_log: the spend record is not editable by the app.
    # CI has no app_user until an earlier migration creates it; this runs after it.
    op.execute("GRANT SELECT, INSERT ON llm_usage TO app_user;")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_llm_usage_tenant_at', table_name='llm_usage')
    op.drop_table('llm_usage')
