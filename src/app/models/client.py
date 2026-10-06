
import uuid
from sqlalchemy import String, UniqueConstraint

from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


# define the clients table
class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("intake_address", name="unique_intake_address"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    # Plus-address on the shared intake mailbox, stored lowercase.
    intake_address: Mapped[str | None] = mapped_column(String(255), nullable=True)



