import datetime
import uuid

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.enums import Role
from app.models.base import Base


class Principal(Base):
    ''' A staff member. Belongs to one tenant: the tenant a request acts on is always
    the caller's, never something the caller sends. '''

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    display_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())


class RoleGrant(Base):
    __tablename__ = "role_grants"
    __table_args__ = (UniqueConstraint("principal_id", "role", name="unique_grant"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    role: Mapped[Role] = mapped_column(SQLEnum(Role, native_enum=False))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    # sha256 of the key. The key itself is shown once at creation and never stored.
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime.datetime | None]
