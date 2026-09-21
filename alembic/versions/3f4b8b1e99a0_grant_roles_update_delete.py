"""grant_roles_update_delete

Revision ID: 3f4b8b1e99a0
Revises: 1513a549af5f
Create Date: 2026-09-20 23:09:41.141546

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f4b8b1e99a0'
down_revision: Union[str, Sequence[str], None] = '1513a549af5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
    CREATE ROLE app_user WITH LOGIN PASSWORD 'password2';
    GRANT USAGE ON SCHEMA public TO app_user;

""")
    op.execute("""
        GRANT SELECT, INSERT ON clients TO app_user;
        GRANT SELECT, INSERT, UPDATE ON documents TO app_user;
        GRANT SELECT, INSERT ON sources TO app_user;
        GRANT SELECT, INSERT ON document_pages TO app_user;
        GRANT SELECT, INSERT ON audit_log TO app_user;
    """)
    


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        REVOKE SELECT, INSERT ON clients FROM app_user;
        REVOKE SELECT, INSERT, UPDATE ON documents FROM app_user;
        REVOKE SELECT, INSERT ON sources FROM app_user;
        REVOKE SELECT, INSERT ON document_pages FROM app_user;
        REVOKE SELECT, INSERT ON audit_log FROM app_user;
    """)
    op.execute("DROP ROLE app_user;")
    
