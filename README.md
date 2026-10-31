# Clinic Booking API

Modular Monolith FastAPI implementation for the Clinic Booking API (`contract/openapi.json`).

## Architecture Overview

- **Presentation Layer (`src/api/`)**: FastAPI routers (`POST /reset`, `GET /slots`, `POST /bookings`, `GET /bookings`, `GET /bookings/{id}`, `DELETE /bookings/{id}`) with global `X-API-Key` security.
- **Business Logic Layer (`src/services/`)**:
  - `SlotService`: Slot reset and availability queries.
  - `BookingService`: Validation, concurrency control, cursor pagination, and lifecycle management.
  - `IdempotencyService`: Redis-backed request deduplication and payload hash comparison (Level 4).
  - `RateLimiterService`: Redis Sorted Set (ZSET) sliding-window rate limiter per email (Level 5).
- **Data Access Layer (`src/repositories/`)**: Async SQLAlchemy repositories with row-level locking (`SELECT ... FOR UPDATE`) and partial unique indexing (`WHERE status = 'confirmed'`).
- **Core Infrastructure (`src/core/`)**: Centralized exception handling (`{"error": "<message>"}`), `pydantic-settings` config, async database engine, and Redis client.

## Quick Start

### Option A: Run the Entire Stack with Docker Compose (Recommended)

Start all services (PostgreSQL, Redis, Backend API, and Frontend UI):

```bash
docker compose up -d
```

- **Frontend UI**: `http://localhost:5173`
- **Backend API & Swagger Docs**: `http://localhost:8000/docs`

### Option B: Local Development

#### 1. Start Infrastructure (PostgreSQL & Redis)

```bash
docker compose up -d postgres redis
```

#### 2. Install Backend Dependencies via `uv`

```bash
cd backend
uv sync
```

#### 3. Run the API Server

```bash
uv run uvicorn src.main:app --reload --port 8000
```

#### 4. Run Automated Tests (Levels 1–5)

```bash
uv run pytest -v
```

#### 5. Run Frontend UI Dashboard (Cal.com Style)

```bash
cd ../frontend
npm install
npm run dev
```

The frontend dashboard will be live at `http://localhost:5173`, proxied to the backend at `http://localhost:8000`.
- **Book Appointment**: UI for provider & slot picker.
- **My Bookings**: Look up appointments by email with cursor pagination or booking ID, with instant cancellation.
- **Admin Operations**: Slot seeding via presets or custom JSON (`POST /reset`) and live inventory inspection.
- **API Security**: The `X-API-Key` is configured strictly via `frontend/.env` (`VITE_API_KEY`) and automatically attached to requests.
