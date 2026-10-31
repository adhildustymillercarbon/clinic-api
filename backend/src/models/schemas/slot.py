from datetime import datetime, timezone
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)


def format_datetime_utc(dt: datetime) -> str:
    """Format datetime object into an ISO-8601 UTC string with 'Z' suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


class SlotSchema(BaseModel):
    """Schema representing an appointment slot."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "id": "s1",
                "provider": "Dr Ade",
                "start_time": "2026-12-10T09:00:00Z",
                "end_time": "2026-12-10T09:30:00Z",
            }
        },
    )

    id: str = Field(
        ...,
        min_length=1,
        description="Unique identifier for the appointment slot",
        examples=["s1"],
    )
    provider: str = Field(
        ...,
        min_length=1,
        description="Name of the doctor or healthcare practitioner",
        examples=["Dr Ade"],
    )
    start_time: datetime = Field(
        ...,
        validation_alias=AliasChoices("start_time", "start"),
        description="Slot starting time in UTC (ISO-8601)",
        examples=["2026-12-10T09:00:00Z"],
    )
    end_time: datetime = Field(
        ...,
        validation_alias=AliasChoices("end_time", "end"),
        description="Slot concluding time in UTC (ISO-8601)",
        examples=["2026-12-10T09:30:00Z"],
    )

    @field_validator("id", "provider")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only strings."""
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def ensure_tz_aware(cls, v: datetime) -> datetime:
        """Ensure datetime has timezone awareness, defaulting to UTC."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_time_order(self) -> "SlotSchema":
        """Ensure slot end time is strictly after start time."""
        if self.end_time <= self.start_time:
            raise ValueError("Slot end time must be after start time")
        return self

    @field_serializer("start_time", "end_time")
    def serialize_dt(self, dt: datetime) -> str:
        """Serialize datetime to UTC ISO string with Z suffix."""
        return format_datetime_utc(dt)

    @computed_field
    @property
    def start(self) -> str:
        """Legacy alias for start_time formatted as UTC string."""
        return format_datetime_utc(self.start_time)

    @computed_field
    @property
    def end(self) -> str:
        """Legacy alias for end_time formatted as UTC string."""
        return format_datetime_utc(self.end_time)


class SlotWithAvailabilitySchema(SlotSchema):
    """Schema representing a slot with booking availability status."""

    available: bool = Field(
        ...,
        description="Whether the slot is currently open for booking (false if an active confirmed booking exists)",
        examples=[True],
    )


class ResetSlotsRequest(BaseModel):
    """Request payload for POST /reset."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "slots": [
                    {
                        "id": "s1",
                        "provider": "Dr Ade",
                        "start": "2026-12-10T09:00:00Z",
                        "end": "2026-12-10T09:30:00Z",
                    },
                    {
                        "id": "s2",
                        "provider": "Dr Ade",
                        "start": "2026-12-10T10:00:00Z",
                        "end": "2026-12-10T10:30:00Z",
                    },
                ]
            }
        }
    )

    slots: list[SlotSchema] = Field(
        ...,
        description="Complete list of appointment slots to seed. All previous bookings and slots will be replaced.",
    )

    @model_validator(mode="after")
    def validate_unique_and_non_overlapping_slots(self) -> "ResetSlotsRequest":
        """Enforce unique slot IDs and globally non-overlapping slot times."""
        seen_ids: set[str] = set()
        for slot in self.slots:
            if slot.id in seen_ids:
                raise ValueError(f"Duplicate slot id '{slot.id}'")
            seen_ids.add(slot.id)

        sorted_slots = sorted(self.slots, key=lambda s: (s.start_time, s.end_time))
        for i in range(1, len(sorted_slots)):
            prev = sorted_slots[i - 1]
            curr = sorted_slots[i]
            if curr.start_time == prev.start_time:
                raise ValueError("Two slots cannot have the same time")
            if curr.start_time < prev.end_time:
                raise ValueError("Slots cannot have overlapping times")
        return self


class SlotListResponse(BaseModel):
    """Response payload for GET /slots."""

    slots: list[SlotWithAvailabilitySchema] = Field(
        ...,
        description="List of appointment slots matching query criteria, ordered chronologically by earliest start time.",
    )
