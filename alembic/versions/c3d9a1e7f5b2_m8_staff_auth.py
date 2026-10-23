"""M8: principals, role grants, API keys

Revision ID: c3d9a1e7f5b2
Revises: 7ab04fb52bf7
Create Date: 2026-10-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d9a1e7f5b2'
down_revision: Union[str, Sequence[str], None] = '7ab04fb52bf7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'principals',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'role_grants',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('principal_id', sa.Uuid(), nullable=False),
        sa.Column('role', sa.String(length=8), nullable=False),
        sa.ForeignKeyConstraint(['principal_id'], ['principals.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('principal_id', 'role', name='unique_grant'),
    )
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('principal_id', sa.Uuid(), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['principal_id'], ['principals.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('hash'),
    )
    op.execute("GRANT SELECT, INSERT ON principals, role_grants, api_keys TO app_user;")
    # UPDATE only to set revoked_at. No DELETE: a revoked key stays as the record of
    # who held access and when.
    op.execute("GRANT UPDATE ON api_keys TO app_user;")
    # Reclassify drops the old type's fields; their values live on in the audit row.
    op.execute("GRANT DELETE ON extracted_fields TO app_user;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE DELETE ON extracted_fields FROM app_user;")
    op.drop_table('api_keys')
    op.drop_table('role_grants')
    op.drop_table('principals')
