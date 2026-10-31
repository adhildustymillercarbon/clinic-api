import asyncio
import base64
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
import uuid
import redis.asyncio as redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    RateLimitExceededError,
    SlotAlreadyBookedError,
    SlotAlreadyStartedError,
    SlotNotFoundError,
    UnprocessableEntityError,
)
from src.models.db.booking import BookingModel
from src.models.schemas.booking import (
    BookingResponse,
    NewBookingPayload,
    PaginatedBookingsResponse,
)
from src.models.schemas.slot import format_datetime_utc
from src.repositories.booking_repository import BookingRepository
from src.repositories.slot_repository import SlotRepository

# In-memory lock dictionary previously used for application-level synchronization.
# Retained for reference and used in the commented-out locked implementation below.
_slot_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class BookingService:
    """Core domain service orchestrating booking lifecycle, concurrency,
    and cursor-based pagination (Levels 1–5).
    """

    def __init__(
        self, session: AsyncSession, redis_client: redis.Redis | None = None
    ) -> None:
        """Initialize BookingService with database session.

        Args:
            session: Active asynchronous SQLAlchemy database session.
            redis_client: Optional Redis client connection (cross-cutting transport concerns
                are handled by API decorators).
        """
        self.session = session
        self.slot_repo = SlotRepository(session)
        self.booking_repo = BookingRepository(session)

    async def create_booking(
        self,
        payload: NewBookingPayload,
    ) -> BookingResponse:
        """Reserve an appointment slot for a patient without pessimistic row locking.

        NOTE: This implementation omits pessimistic row-level locking (SELECT ... FOR UPDATE)
        and in-memory mutexes (_slot_locks). Concurrent requests for the same slot can read
        the slot simultaneously before either transaction commits, resulting in race conditions
        where duplicate bookings are created for the same slot.

        Args:
            payload: Validated patient booking payload.

        Returns:
            The created booking response model.

        Raises:
            SlotNotFoundError: When slot does not exist (HTTP 404).
            SlotAlreadyStartedError: When slot start time has already passed (HTTP 400).
            SlotAlreadyBookedError: When slot is already booked (HTTP 409).
        """
        # Fetch slot WITHOUT row-level lock (no SELECT ... FOR UPDATE)
        slot = await self.slot_repo.get_slot(payload.slot_id)
        if slot is None:
            raise SlotNotFoundError("Slot not found")

        now = datetime.now(timezone.utc)
        already_booked = await self.booking_repo.has_confirmed_booking(
            payload.slot_id
        )
        # Delegate domain rules to SlotModel aggregate
        slot.assert_bookable(now=now, already_booked=already_booked)
        
       # Simulate real-world external I/O (payment, notifications, verification)
        await asyncio.sleep(0.1)

        # Create confirmed booking record
        booking = BookingModel(
            id=f"b_{uuid.uuid4().hex[:12]}",
            slot_id=payload.slot_id,
            name=payload.name,
            email=payload.email,
            status="confirmed",
            created_at=now,
        )

        try:
            await self.booking_repo.create_booking(booking)
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise SlotAlreadyBookedError("Slot is already booked") from exc

        return BookingResponse.model_validate(booking)

    # =========================================================================
    # PESSIMISTIC ROW-LEVEL LOCKING IMPLEMENTATION (COMMENTED OUT)
    # The following method implements pessimistic row-level locking via
    # `SlotRepository.get_slot_for_update` and asyncio mutex serialization.
    # In this locked version, concurrent requests for the same slot ID are serialized
    # and blocked until the first transaction commits or rolls back, preventing
    # race conditions and duplicate bookings.
    # =========================================================================
    # async def create_booking_with_pessimistic_lock(
    #     self,
    #     payload: NewBookingPayload,
    # ) -> BookingResponse:
    #     """Reserve an appointment slot using pessimistic row-level locking.
    
    #     Acquires an exclusive lock on the slot row in the database (SELECT ... FOR UPDATE)
    #     and an asyncio.Lock mutex to serialize concurrent attempts.
    #     """
    #     # Acquire lock for the targeted slot to serialize concurrent requests
    #     async with _slot_locks[payload.slot_id]:
    #         # Lock the slot row in the database with SELECT ... FOR UPDATE
    #         slot = await self.slot_repo.get_slot_for_update(payload.slot_id)
    #         if slot is None:
    #             raise SlotNotFoundError("Slot not found")
    
    #         now = datetime.now(timezone.utc)
    #         already_booked = await self.booking_repo.has_confirmed_booking(
    #             payload.slot_id
    #         )
    #         # Delegate domain rules to SlotModel aggregate
    #         slot.assert_bookable(now=now, already_booked=already_booked)
    
    #         # Create confirmed booking record
    #         booking = BookingModel(
    #             id=f"b_{uuid.uuid4().hex[:12]}",
    #             slot_id=payload.slot_id,
    #             name=payload.name,
    #             email=payload.email,
    #             status="confirmed",
    #             created_at=now,
    #         )
    
    #         try:
    #             await self.booking_repo.create_booking(booking)
    #             await self.session.commit()
    #         except IntegrityError as exc:
    #             await self.session.rollback()
    #             raise SlotAlreadyBookedError("Slot is already booked") from exc
    
    #         return BookingResponse.model_validate(booking)

    async def get_booking(self, booking_id: str) -> BookingResponse:
        """Retrieve a booking by its unique identifier (Level 1).

        Args:
            booking_id: The unique ID of the booking to fetch.

        Returns:
            The booking response model.

        Raises:
            NotFoundError: If no booking exists with the given ID (HTTP 404).
        """
        booking = await self.booking_repo.get_booking_by_id(booking_id)
        if booking is None:
            raise NotFoundError("No booking with that id")
        return BookingResponse.model_validate(booking)

    async def cancel_booking(self, booking_id: str) -> BookingResponse:
        """Cancel an existing booking and free its associated slot (Level 3).

        Args:
            booking_id: The unique ID of the booking to cancel.

        Returns:
            The updated booking model with status set to 'cancelled'.

        Raises:
            NotFoundError: If no booking exists with the given ID (HTTP 404).
            ConflictError: If the booking was already cancelled (HTTP 409).
        """
        booking = await self.booking_repo.get_booking_by_id(
            booking_id, for_update=True
        )
        if booking is None:
            raise NotFoundError("No booking with that id")

        if booking.status == "cancelled":
            raise ConflictError("Booking is already cancelled")

        # Mark as cancelled and release slot for new bookings
        booking.status = "cancelled"
        await self.session.commit()
        await self.session.refresh(booking)
        return BookingResponse.model_validate(booking)

    async def list_bookings(
        self,
        email: str | None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> PaginatedBookingsResponse:
        """List bookings for a specific person using cursor pagination (Level 4).

        Results are ordered oldest first (by created_at ASC, id ASC).
        Email matching is case-insensitive.

        Args:
            email: Required email query parameter.
            limit: Page size between 1 and 50 (defaults to 10).
            cursor: Opaque cursor string received from next_cursor of previous page.

        Returns:
            PaginatedBookingsResponse containing the list of bookings and next_cursor.

        Raises:
            BadRequestError: If email is missing, limit is outside [1, 50], or cursor is invalid.
        """
        if email is None or not email.strip():
            raise BadRequestError("email query parameter is required")

        if limit < 1 or limit > 50:
            raise BadRequestError("limit must be between 1 and 50")

        cursor_created_at: datetime | None = None
        cursor_id: str | None = None

        if cursor is not None and cursor != "":
            cursor_created_at, cursor_id = self._decode_cursor(cursor)

        # Query database for limit + 1 items to determine if a next page exists
        rows = await self.booking_repo.list_bookings_by_email(
            email=email,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )

        has_more = len(rows) > limit
        page_rows = rows[:limit]

        next_cursor: str | None = None
        if has_more and page_rows:
            last_row = page_rows[-1]
            next_cursor = self._encode_cursor(last_row.created_at, last_row.id)

        return PaginatedBookingsResponse(
            bookings=[BookingResponse.model_validate(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    @staticmethod
    def _encode_cursor(created_at: datetime, booking_id: str) -> str:
        """Encode timestamp and booking ID into an opaque, URL-safe base64 cursor.

        Args:
            created_at: Datetime of the last booking on current page.
            booking_id: Unique ID of the last booking on current page.

        Returns:
            URL-safe base64 encoded cursor string.
        """
        raw = f"{format_datetime_utc(created_at)}|{booking_id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, str]:
        """Decode and validate an opaque URL-safe base64 cursor string.

        Args:
            cursor: URL-safe base64 encoded cursor string.

        Returns:
            Tuple of (parsed_datetime, booking_id).

        Raises:
            BadRequestError: If cursor format is invalid or cannot be decoded.
        """
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            ts_str, booking_id = decoded.split("|", 1)
            if not booking_id:
                raise ValueError("Missing booking id in cursor")
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt, booking_id
        except Exception as exc:
            raise BadRequestError("Invalid cursor") from exc
