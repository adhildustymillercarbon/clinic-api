from sqlalchemy import case, delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BadRequestError
from src.models.db.booking import BookingModel
from src.models.db.slot import SlotModel
from src.models.schemas.slot import SlotSchema, SlotWithAvailabilitySchema


class SlotRepository:
    """Data access repository for slot persistence and availability queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_all_slots(self, slots: list[SlotSchema]) -> None:
        """Delete all existing bookings and slots, then insert new slot records."""
        await self.session.execute(delete(BookingModel))
        await self.session.execute(delete(SlotModel))
        await self.session.flush()

        if slots:
            db_slots = [
                SlotModel(
                    id=slot.id,
                    provider=slot.provider,
                    start_time=slot.start_time,
                    end_time=slot.end_time,
                )
                for slot in slots
            ]
            self.session.add_all(db_slots)

        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise BadRequestError("Duplicate slot id or time conflict") from exc

    async def list_slots_with_availability(
        self, provider: str | None = None
    ) -> list[SlotWithAvailabilitySchema]:
        """Fetch slots sorted earliest start first, with computed confirmed availability."""
        confirmed_exists = (
            select(BookingModel.id)
            .where(
                BookingModel.slot_id == SlotModel.id,
                BookingModel.status == "confirmed",
            )
            .correlate(SlotModel)
            .exists()
        )

        available_expr = case((confirmed_exists, False), else_=True).label("available")

        stmt = select(SlotModel, available_expr).order_by(
            SlotModel.start_time.asc(), SlotModel.id.asc()
        )
        if provider is not None:
            stmt = stmt.where(SlotModel.provider == provider)

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            SlotWithAvailabilitySchema(
                id=slot.id,
                provider=slot.provider,
                start_time=slot.start_time,
                end_time=slot.end_time,
                available=bool(is_available),
            )
            for slot, is_available in rows
        ]
        
    async def get_slot_for_update(self, slot_id: str) -> SlotModel | None:
       """Fetch a slot row WITH pessimistic row locking."""
       stmt = (
           select(SlotModel)
           .where(SlotModel.id == slot_id)
           .with_for_update()
           )
       result = await self.session.execute(stmt)
       return result.scalar_one_or_none()

