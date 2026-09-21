"""enforce_audit_append

Revision ID: 1513a549af5f
Revises: 2aa65307bf4d
Create Date: 2026-09-20 09:55:58.515412

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1513a549af5f'
down_revision: Union[str, Sequence[str], None] = '2aa65307bf4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;")
    pass


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("GRANT UPDATE, DELETE ON audit_log TO PUBLIC;")
    pass
