"""M13: jobs.locked_by, the owner of a RUNNING job

Revision ID: b4d9f1a7c260
Revises: a2c8e5d1b937
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d9f1a7c260'
down_revision: Union[str, Sequence[str], None] = 'a2c8e5d1b937'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no backfill: a job RUNNING at deploy has no owner, so no worker can
    # finish it; the sweep returns it to PENDING after one lease. app_user's UPDATE on
    # jobs is table-wide and covers the new column.
    op.add_column('jobs', sa.Column('locked_by', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('jobs', 'locked_by')
