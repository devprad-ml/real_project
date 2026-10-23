"""M9: members (MPI), associations, documents.route_queue

Revision ID: d8a4f2c6e913
Revises: c3d9a1e7f5b2
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8a4f2c6e913'
down_revision: Union[str, Sequence[str], None] = 'c3d9a1e7f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'members',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('member_id', sa.String(length=32), nullable=False),
        sa.Column('first_name', sa.String(length=100), nullable=True),
        sa.Column('last_name', sa.String(length=100), nullable=True),
        sa.Column('dob', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'member_id', name='unique_tenant_member'),
    )
    op.create_table(
        'associations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('member_pk', sa.Uuid(), nullable=False),
        sa.Column('match_confidence', sa.Float(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['clients.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['member_pk'], ['members.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id'),
    )
    op.add_column(
        'documents',
        sa.Column(
            'route_queue',
            sa.Enum(
                'CLAIMS_OPS', 'APPEALS', 'UTILIZATION_MGMT', 'MEMBER_SERVICES',
                name='routequeue', native_enum=False,
            ),
            nullable=True,
        ),
    )
    # UPDATE on members: the CSV loader upserts. No DELETE on either: a member a
    # document was bound to must outlive the next export that drops them.
    op.execute("GRANT SELECT, INSERT, UPDATE ON members TO app_user;")
    op.execute("GRANT SELECT, INSERT ON associations TO app_user;")
    # Documents M7/M8 left at EXTRACTED were waiting for M9 with no job behind them.
    # Names, not values: native_enum=False stores the member name.
    op.execute(
        """
        INSERT INTO jobs (id, tenant_id, document_id, kind, state, attempts, run_after)
        SELECT gen_random_uuid(), tenant_id, id, 'ASSOCIATE', 'PENDING', 0, now()
        FROM documents WHERE status = 'EXTRACTED'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The pre-M9 code has no handler for these kinds and no notion of the statuses
    # after EXTRACTED; put everything back where M8 left it. The audit trail keeps
    # the record of what M9 did.
    op.execute(
        "UPDATE jobs SET state = 'ERROR', last_error = 'M9 downgraded' "
        "WHERE kind IN ('ASSOCIATE', 'ROUTE') AND state IN ('PENDING', 'RUNNING')"
    )
    op.execute(
        "UPDATE documents SET status = 'EXTRACTED' "
        "WHERE status IN ('ASSOCIATED', 'ROUTED')"
    )
    op.drop_column('documents', 'route_queue')
    op.drop_table('associations')
    op.drop_table('members')
