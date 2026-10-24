"""M10: replaceable associations, backfill push jobs

Revision ID: e1b7c3d9a524
Revises: d8a4f2c6e913
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e1b7c3d9a524'
down_revision: Union[str, Sequence[str], None] = 'd8a4f2c6e913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A document rejected downstream comes back through associate, which replaces its
    # binding. The audit rows keep every member it was ever bound to.
    op.execute("GRANT DELETE ON associations TO app_user;")
    # Documents M9 left at ROUTED were waiting for M10 with no job behind them.
    op.execute(
        """
        INSERT INTO jobs (id, tenant_id, document_id, kind, state, attempts, run_after)
        SELECT gen_random_uuid(), tenant_id, id, 'PUSH', 'PENDING', 0, now()
        FROM documents WHERE status = 'ROUTED'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE jobs SET state = 'ERROR', last_error = 'M10 downgraded' "
        "WHERE kind = 'PUSH' AND state IN ('PENDING', 'RUNNING')"
    )
    op.execute("UPDATE documents SET status = 'ROUTED' WHERE status = 'PUSHED'")
    op.execute("REVOKE DELETE ON associations FROM app_user;")
