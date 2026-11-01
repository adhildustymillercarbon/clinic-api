from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from fastapi.responses import JSONResponse
import redis.asyncio as redis

from src.api.decorators.idempotency import idempotent_action
from src.api.deps import get_booking_service
from src.core.exceptions import BadRequestError
from src.core.redis import get_redis_client
from src.models.schemas.booking import (
    BookingResponse,
    NewBookingPayload,
    PaginatedBookingsResponse,
)
from src.services.booking_service import BookingService

router = APIRouter(tags=["Bookings"])


@router.post(
    "/bookings",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve an appointment slot",
    response_description="Booking reservation confirmed and slot locked",
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "Patient booking reservation payload.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["slot_id", "name", "email"],
                        "properties": {
                            "slot_id": {
                                "type": "string",
                                "description": "Unique identifier of the appointment slot to reserve",
                                "example": "s1",
                            },
                            "name": {
                                "type": "string",
                                "description": "Full name of the patient (whitespace will be trimmed)",
                                "example": "Ada Obi",
                            },
                            "email": {
                                "type": "string",
                                "format": "email",
                                "description": "Valid patient email address (case-insensitive, stored lowercase)",
                                "example": "ada@example.com",
                            },
                        },
                    },
                    "examples": {
                        "standard_booking": {
                            "summary": "Sample Patient Booking",
                            "value": {
                                "slot_id": "s1",
                                "name": "Ada Obi",
                                "email": "ada@example.com",
                            },
                        }
                    },
                }
            },
        }
    },
    responses={
        201: {
            "description": "Booking created successfully with status 'confirmed'.",
        },
        400: {
            "description": "Validation error: Missing or empty fields, malformed email, or slot has already started.",
            "content": {
                "application/json": {
                    "example": {"error": "Slot has already started"}
                }
            },
        },
        401: {
            "description": "Missing or invalid X-API-Key header.",
            "content": {
                "application/json": {
                    "example": {"error": "Missing or invalid API key"}
                }
            },
        },
        404: {
            "description": "The specified slot_id was not found in the clinic database.",
            "content": {
                "application/json": {
                    "example": {"error": "Slot not found"}
                }
            },
        },
        409: {
            "description": "Concurrency Conflict: Slot already has an active confirmed booking.",
            "content": {
                "application/json": {
                    "example": {"error": "Slot is already booked"}
                }
            },
        },
        422: {
            "description": "Idempotency Mismatch: Idempotency-Key header was previously used with a different request body.",
            "content": {
                "application/json": {
                    "example": {"error": "Idempotency key reused with different payload"}
                }
            },
        },
        429: {
            "description": "Rate Limit Exceeded: Exceeded 5 booking attempts per minute for this email.",
            "headers": {
                "Retry-After": {
                    "description": "Number of seconds to wait before retrying",
                    "schema": {"type": "integer", "example": 45},
                }
            },
            "content": {
                "application/json": {
                    "example": {"error": "Rate limit exceeded. Try again in 45 seconds"}
                }
            },
        },
    },
)
@idempotent_action(key_header="Idempotency-Key", rate_limit_field="email")
async def create_booking(
    request: Request,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional unique identifier (e.g., UUIDv4) for safe retries without duplicate bookings.",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
    service: BookingService = Depends(get_booking_service),
    redis_client: redis.Redis = Depends(get_redis_client),
) -> BookingResponse:
    """Reserve an appointment slot for a patient with built-in concurrency safety.

    ### How to leverage this endpoint:
    Use this endpoint to book a doctor consultation slot retrieved from `GET /slots`.

    - **Concurrency & Race Conditions**: Uses pessimistic database row locks (`SELECT FOR UPDATE`). When multiple clients attempt to book the exact same slot concurrently, exactly one succeeds (`201`) and all others receive `409 Conflict`.
    - **Safe Retries (Idempotency)**: Include an `Idempotency-Key` header with a client-generated UUID. If network drops occur, replay the exact same request safely; the server will return the previously cached response without re-executing booking logic. Sending the same key with altered payload fields returns `422 Unprocessable Entity`.
    - **Sliding-Window Rate Limiting**: Capped at **5 booking attempts per email per minute** (both successful and failed requests count). Exceeding this limit returns `429 Too Many Requests` accompanied by a `Retry-After: <seconds>` HTTP header.
    - **Data Normalization**: Patient name and email are trimmed of surrounding whitespace; emails are normalized to lowercase. Past slots are rejected with `400 Bad Request`.

    #### Required Headers:
    - `X-API-Key`: Your configured clinic API secret key.
    - `Idempotency-Key` *(Optional)*: Recommended for production retry resilience.

    #### Example cURL:
    ```bash
    curl -X POST "http://localhost:8000/bookings" \\
      -H "X-API-Key: secret123" \\
      -H "Idempotency-Key: 9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d" \\
      -H "Content-Type: application/json" \\
      -d '{
        "slot_id": "s1",
        "name": "Ada Obi",
        "email": "ada@example.com"
      }'
    ```
    """
    raw_payload = getattr(request.state, "raw_payload", None)
    if raw_payload is None:
        try:
            raw_payload = await request.json()
        except Exception as exc:
            raise BadRequestError("Invalid JSON request body") from exc

    payload = NewBookingPayload.validate_raw(raw_payload)
    return await service.create_booking(payload)


@router.get(
    "/bookings",
    response_model=PaginatedBookingsResponse,
    status_code=status.HTTP_200_OK,
    summary="List patient bookings with cursor pagination",
    response_description="Chronological page of patient bookings and next page cursor",
    responses={
        200: {
            "description": "Page of bookings matching patient email returned successfully.",
        },
        400: {
            "description": "Missing email query parameter, invalid limit (must be 1–50), or invalid cursor format.",
            "content": {
                "application/json": {
                    "example": {"error": "email query parameter is required"}
                }
            },
        },
        401: {
            "description": "Missing or invalid X-API-Key header.",
            "content": {
                "application/json": {
                    "example": {"error": "Missing or invalid API key"}
                }
            },
        },
    },
)
async def list_bookings(
    email: str | None = Query(
        default=None,
        description="Patient email address (required, matched case-insensitively).",
        examples=["ada@example.com"],
    ),
    limit: int = Query(
        default=10,
        description="Number of bookings to retrieve per page (integer between 1 and 50).",
        examples=[10],
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque base64 cursor token received from `next_cursor` of the previous page response.",
        examples=["MjAyNi0xMC0yOVQxMTowMDowMFp8Yl9hMWIyYzNkNGU1ZjY="],
    ),
    service: BookingService = Depends(get_booking_service),
) -> PaginatedBookingsResponse:
    """Retrieve chronological bookings for a patient using stable cursor pagination.

    ### How to leverage this endpoint:
    Use this endpoint to view a patient's booking history (both `confirmed` and `cancelled`).

    - **Cursor Pagination Loop**:
      1. First page: Call `GET /bookings?email=patient@example.com&limit=10`.
      2. If `next_cursor` is present, fetch next page with `GET /bookings?email=patient@example.com&limit=10&cursor=<next_cursor>`.
      3. When `next_cursor` is `null`, you have reached the end of the history.
    - **Chronological Ordering**: Items are always sorted oldest first (`created_at` ASC, `id` ASC).
    - **Case-Insensitive Filter**: Email matching ignores case differences (`Ada@Example.com` matches `ada@example.com`).

    #### Required Headers:
    - `X-API-Key`: Your configured clinic API secret key.

    #### Example cURL:
    ```bash
    curl -X GET "http://localhost:8000/bookings?email=ada@example.com&limit=10" \\
      -H "X-API-Key: secret123"
    ```
    """
    return await service.list_bookings(email=email, limit=limit, cursor=cursor)


@router.get(
    "/bookings/{id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve booking details",
    response_description="Detailed booking reservation object",
    responses={
        200: {
            "description": "Booking details retrieved successfully.",
        },
        401: {
            "description": "Missing or invalid X-API-Key header.",
            "content": {
                "application/json": {
                    "example": {"error": "Missing or invalid API key"}
                }
            },
        },
        404: {
            "description": "No booking was found matching the provided id.",
            "content": {
                "application/json": {
                    "example": {"error": "No booking with that id"}
                }
            },
        },
    },
)
async def get_booking(
    booking_id: str = Path(
        ...,
        alias="id",
        description="Unique identifier of the booking reservation (e.g., 'b_a1b2c3d4e5f6').",
        examples=["b_a1b2c3d4e5f6"],
    ),
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    """Retrieve full details of a specific booking by its identifier.

    ### How to leverage this endpoint:
    Use this endpoint to verify booking details or confirm cancellation status.

    - **Status Values**: The `status` field indicates whether the booking is currently active (`confirmed`) or was cancelled (`cancelled`).

    #### Required Headers:
    - `X-API-Key`: Your configured clinic API secret key.

    #### Example cURL:
    ```bash
    curl -X GET "http://localhost:8000/bookings/b_a1b2c3d4e5f6" \\
      -H "X-API-Key: secret123"
    ```
    """
    return await service.get_booking(booking_id=booking_id)


@router.delete(
    "/bookings/{id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel a booking and release slot",
    response_description="Booking updated to 'cancelled' status and slot released",
    responses={
        200: {
            "description": "Booking successfully cancelled and slot released back to available inventory.",
        },
        401: {
            "description": "Missing or invalid X-API-Key header.",
            "content": {
                "application/json": {
                    "example": {"error": "Missing or invalid API key"}
                }
            },
        },
        404: {
            "description": "No booking was found matching the provided id.",
            "content": {
                "application/json": {
                    "example": {"error": "No booking with that id"}
                }
            },
        },
        409: {
            "description": "Conflict: This booking has already been cancelled previously.",
            "content": {
                "application/json": {
                    "example": {"error": "Booking is already cancelled"}
                }
            },
        },
    },
)
async def cancel_booking(
    booking_id: str = Path(
        ...,
        alias="id",
        description="Unique identifier of the booking reservation to cancel.",
        examples=["b_a1b2c3d4e5f6"],
    ),
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    """Cancel an active booking and release its appointment slot for other patients.

    ### How to leverage this endpoint:
    Use this endpoint when a patient cancels or reschedules their appointment.

    - **Immediate Slot Release**: Sets the booking's `status` to `cancelled` and instantly makes its associated slot available (`available: true`) for booking via `POST /bookings`.
    - **Cancellation Guard**: Attempting to delete a booking that is already cancelled returns `409 Conflict`.

    #### Required Headers:
    - `X-API-Key`: Your configured clinic API secret key.

    #### Example cURL:
    ```bash
    curl -X DELETE "http://localhost:8000/bookings/b_a1b2c3d4e5f6" \\
      -H "X-API-Key: secret123"
    ```
    """
    return await service.cancel_booking(booking_id=booking_id)
