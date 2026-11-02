from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.router import api_router
from src.core.config import get_settings
from src.core.database import close_db, init_db
from src.core.error_webhook import setup_error_webhook
from src.core.exceptions import register_exception_handlers
from src.core.redis import close_redis, get_redis_client
from src.core.telemetry import setup_telemetry, shutdown_telemetry


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    await init_db()
    yield
    if hasattr(_app.state, "error_webhook"):
        await _app.state.error_webhook.shutdown()
    shutdown_telemetry()
    await close_redis()
    await close_db()


TAGS_METADATA = [
    {
        "name": "Slots",
        "description": "Query available and scheduled doctor consultation slots with real-time booking availability.",
    },
    {
        "name": "Bookings",
        "description": "Manage appointment reservations with high-concurrency race protection, idempotency retries, sliding-window rate limiting, and cursor pagination.",
    },
    {
        "name": "Reset",
        "description": "Administrative maintenance endpoint to seed schedule slots, wipe active bookings, and flush cache states.",
    },
]

API_DESCRIPTION = """
### Clinic Booking API Developer Guide

Welcome to the **Clinic Booking API**. This service enables client applications to discover doctor appointment slots, book appointments with robust concurrency safety, and manage booking lifecycles.

---

### Core Architectural Features

1. **Authentication**:
   - All endpoints require an **`X-API-Key`** header.
   - Invalid or missing API keys immediately result in `401 Unauthorized`.

2. **Atomic Concurrency Control**:
   - Appointment slots are protected against double-booking using pessimistic row-level locking.
   - If multiple patients attempt to book the same slot simultaneously, exactly one reservation succeeds (`201 Created`) and subsequent contenders receive `409 Conflict`.

3. **Safe Retries with Idempotency**:
   - Pass an **`Idempotency-Key`** header with `POST /bookings` to safely retry requests over unstable network connections without duplicate charges or bookings.
   - Repeating a request with the same key and payload returns the identical response.
   - Changing the payload under an active key returns `422 Unprocessable Entity`.

4. **Sliding-Window Rate Limiting**:
   - Booking creation is throttled to **5 attempts per minute per email**.
   - The 6th attempt returns `429 Too Many Requests` along with a **`Retry-After`** header indicating the number of seconds to wait.

5. **Cursor-Based Pagination**:
   - `GET /bookings` uses stable, URL-safe cursor tokens ordered chronologically (oldest first).
   - Use `next_cursor` from the response to fetch the subsequent page until `next_cursor` is `null`.

6. **Unified Error Contract**:
   - All errors adhere to a consistent JSON envelope:
     ```json
     {
       "error": "Descriptive error message"
     }
     ```
"""


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    app = FastAPI(
        title="Clinic Booking API",
        version=settings.app_version,
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )

    setup_telemetry(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
    )

    register_exception_handlers(app)
    setup_error_webhook(
        app,
        redis_getter=lambda: app.dependency_overrides.get(
            get_redis_client, get_redis_client
        )(),
    )
    app.include_router(api_router)
    return app


app = create_app()
