"""Drop llm_usage: Claude spend and latency now come from Langfuse

Revision ID: d5a1c8e3f792
Revises: c7f3a9d2e481
Create Date: 2026-11-07
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd5a1c8e3f792'
down_revision: Union[str, Sequence[str], None] = 'c7f3a9d2e481'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('llm_usage')


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError(
        "The ledger's rows are gone: restore a database backup instead."
    )
