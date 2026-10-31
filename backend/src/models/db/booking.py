from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class BookingModel(Base):
    """SQLAlchemy model representing a patient booking for a slot."""

    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slot_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("slots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    slot = relationship("SlotModel", back_populates="bookings")

    __table_args__ = (
        # Unique index relaxed (unique=False) so that concurrent requests without row-level locks
        # can create duplicate confirmed bookings in the database.
        Index(
            "ix_bookings_confirmed_slot",
            "slot_id",
            unique=False,
            postgresql_where=text("status = 'confirmed'"),
            sqlite_where=text("status = 'confirmed'"),
        ),
        Index("ix_bookings_email_created_id", "email", "created_at", "id"),
    )
