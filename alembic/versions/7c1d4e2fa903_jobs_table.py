"""jobs table + claim index + app_user grants

Revision ID: 7c1d4e2fa903
Revises: 3f4b8b1e99a0
Create Date: 2026-10-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1d4e2fa903'
down_revision: Union[str, Sequence[str], None] = '3f4b8b1e99a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum('NORMALIZE', 'OCR', 'EMBED', name='jobkind', native_enum=False),
            nullable=False,
        ),
        sa.Column(
            'state',
            sa.Enum(
                'PENDING', 'RUNNING', 'DONE', 'ERROR',
                name='jobstate', native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('run_after', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    # claim_one only ever scans pending rows; done/error rows accumulate forever and
    # would bloat a full index. Predicate uses the enum NAME because
    # SQLEnum(native_enum=False) stores names, not values.
    op.create_index(
        'ix_jobs_claim', 'jobs', ['run_after'], postgresql_where=sa.text("state = 'PENDING'")
    )
    # The 3f4b8b1e99a0 grants predate this table. No DELETE: jobs are history.
    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs TO app_user;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE SELECT, INSERT, UPDATE ON jobs FROM app_user;")
    op.drop_index('ix_jobs_claim', table_name='jobs')
    op.drop_table('jobs')
