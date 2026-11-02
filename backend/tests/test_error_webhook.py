from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI
import httpx
import pytest
import redis.asyncio as redis

from src.core.error_webhook import (
    WebhookSettings,
    setup_error_webhook,
)
from src.core.exceptions import (
    NotFoundError,
    RateLimitExceededError,
    UnauthorizedError,
    register_exception_handlers,
)
from src.core.redis import get_redis_client


class _DummyRedis:
    """In-memory async Redis stand-in for testing SETNX deduplication."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True


def _build_test_app() -> FastAPI:
    """Create a FastAPI app wired with the clinic-booking exception handlers."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/bookings")
    async def create_booking_route() -> dict[str, str]:
        raise RuntimeError("Unexpected runtime invariant breach in booking flow")

    @app.get("/slots")
    async def list_slots_route() -> dict[str, str]:
        raise ConnectionError("Redis cluster down during slot query")

    @app.get("/bookings/{booking_id}")
    async def get_booking_route(booking_id: str) -> dict[str, str]:
        raise NotFoundError(f"Booking {booking_id} not found")

    @app.get("/protected")
    async def protected_route() -> dict[str, str]:
        raise UnauthorizedError("Missing or invalid API key")

    @app.post("/rate-limited")
    async def rate_limited_route() -> dict[str, str]:
        raise RateLimitExceededError(
            "Rate limit exceeded. Try again in 45 seconds", retry_after=45
        )

    return app


def test_webhook_settings_bearer_token_normalization() -> None:
    s1 = WebhookSettings(
        url="https://hooks.example.com/err", bearer_token="raw-secret-123"
    )
    assert s1.build_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer raw-secret-123",
    }

    s2 = WebhookSettings(
        url="https://hooks.example.com/err", bearer_token="Bearer already-prefixed"
    )
    assert s2.build_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer already-prefixed",
    }


def test_booking_service_halt_dispatches_error_webhook() -> None:
    """When a route/service halts with an unhandled RuntimeError, webhook receives ERROR payload."""

    async def _run() -> None:
        captured_requests: list[httpx.Request] = []

        async def mock_webhook_receiver(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json={"ok": True})

        dummy_redis = _DummyRedis()
        app = _build_test_app()
        dispatcher = setup_error_webhook(
            app,
            url="https://webhook.example.com/ingest",
            bearer_token="my-bearer-secret",
            redis_getter=lambda: dummy_redis,
            transport=httpx.MockTransport(mock_webhook_receiver),
            throttle_seconds=60,
            app_package_marker="/tests/",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as ac:
            resp = await ac.post(
                "/bookings",
                json={"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
            )
            assert resp.status_code == 500
            assert resp.json() == {"error": "Internal server error"}

        await dispatcher.shutdown()

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.headers["Authorization"] == "Bearer my-bearer-secret"
        body = json.loads(req.content.decode("utf-8"))
        assert set(body.keys()) == {"level", "message", "module", "stack_trace"}
        assert body["level"] == "ERROR"
        assert (
            "RuntimeError: Unexpected runtime invariant breach" in body["message"]
        )
        assert "test_error_webhook.create_booking_route" in body["module"]
        assert "Unexpected runtime invariant breach" in body["stack_trace"]

    asyncio.run(_run())


def test_slot_service_halt_and_redis_down_fallback() -> None:
    """Even when Redis itself is down, connectivity crash is delivered as CRITICAL and deduplicated locally."""

    async def _run() -> None:
        captured_payloads: list[dict[str, str]] = []

        async def mock_webhook_receiver(request: httpx.Request) -> httpx.Response:
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200)

        def broken_redis_getter() -> Any:
            raise ConnectionError("Redis is unreachable")

        app = _build_test_app()
        dispatcher = setup_error_webhook(
            app,
            url="https://webhook.example.com/ingest",
            bearer_token="secret-token",
            redis_getter=broken_redis_getter,
            transport=httpx.MockTransport(mock_webhook_receiver),
            throttle_seconds=60,
            app_package_marker="/tests/",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as ac:
            r1 = await ac.get("/slots")
            r2 = await ac.get("/slots")
            assert r1.status_code == 500
            assert r2.status_code == 500

        await dispatcher.shutdown()

        assert len(captured_payloads) == 1
        assert captured_payloads[0]["level"] == "CRITICAL"
        assert "ConnectionError: Redis cluster down" in captured_payloads[0]["message"]

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_natural_all_levels_simulation_in_real_app(
    client: httpx.AsyncClient,
) -> None:
    """End-to-end natural simulation of WARNING, ERROR, and CRITICAL against the full app stack."""
    captured_payloads: list[dict[str, str]] = []

    async def mock_webhook_receiver(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer live-sim-token"
        captured_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"received": True})

    app: FastAPI = client._transport.app  # type: ignore[attr-defined]
    dispatcher = setup_error_webhook(
        app,
        url="https://webhook.example.com/ingest",
        bearer_token="live-sim-token",
        redis_getter=lambda: app.dependency_overrides.get(
            get_redis_client, get_redis_client
        )(),
        transport=httpx.MockTransport(mock_webhook_receiver),
        throttle_seconds=60,
    )

    # Seed a valid future slot first
    reset_resp = await client.post(
        "/reset",
        json={
            "slots": [
                {
                    "id": "s1",
                    "provider": "Dr. Smith",
                    "start": "2027-06-15T09:00:00Z",
                    "end": "2027-06-15T09:30:00Z",
                }
            ]
        },
    )
    assert reset_resp.status_code == 200

    # -------------------------------------------------------------------------
    # 1. Natural WARNING Simulation:
    #    - 401 Unauthorized (bad API key)
    #    - 422 UnprocessableEntity (Idempotency-Key reused with altered payload)
    # -------------------------------------------------------------------------
    r_401 = await client.get("/slots", headers={"X-API-Key": "invalid-token"})
    assert r_401.status_code == 401

    r_book = await client.post(
        "/bookings",
        headers={"Idempotency-Key": "idem-natural-test"},
        json={"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
    )
    assert r_book.status_code == 201

    # Replay same Idempotency-Key with different email -> natural 422
    r_422 = await client.post(
        "/bookings",
        headers={"Idempotency-Key": "idem-natural-test"},
        json={"slot_id": "s1", "name": "Ada Obi", "email": "different@example.com"},
    )
    assert r_422.status_code == 422

    # -------------------------------------------------------------------------
    # 2. Natural ERROR Simulation:
    #    Trigger the real IntegrityError fallback inside BookingService
    # -------------------------------------------------------------------------
    from unittest.mock import AsyncMock, patch
    from src.repositories.booking_repository import BookingRepository

    # If has_confirmed_booking returns False during a race, Postgres/SQLite unique index
    # naturally raises sqlalchemy.exc.IntegrityError on commit inside BookingService!
    with patch.object(
        BookingRepository,
        "has_confirmed_booking",
        new=AsyncMock(return_value=False),
    ):
        r_race = await client.post(
            "/bookings",
            json={"slot_id": "s1", "name": "Contender", "email": "race@example.com"},
        )
        assert r_race.status_code == 409

    # -------------------------------------------------------------------------
    # 3. Natural CRITICAL Simulation:
    #    Point Redis to an unreachable port so SlotService.reset_all naturally
    #    halts with redis.exceptions.ConnectionError!
    # -------------------------------------------------------------------------
    dead_redis = redis.from_url(
        "redis://127.0.0.1:6399/0",
        socket_connect_timeout=0.1,
        decode_responses=True,
    )
    orig_override = app.dependency_overrides[get_redis_client]
    app.dependency_overrides[get_redis_client] = lambda: dead_redis
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
            headers={"X-API-Key": client.headers["X-API-Key"]},
        ) as raw_client:
            r_crit = await raw_client.post("/reset", json={"slots": []})
            assert r_crit.status_code == 500
    finally:
        app.dependency_overrides[get_redis_client] = orig_override
        await dead_redis.aclose()

    await dispatcher.shutdown()

    levels = [p["level"] for p in captured_payloads]
    assert "WARNING" in levels
    assert "ERROR" in levels
    assert "CRITICAL" in levels

    # Verify WARNING payloads (401 + 422)
    warnings = [p for p in captured_payloads if p["level"] == "WARNING"]
    assert any("UnauthorizedError" in w["message"] for w in warnings)
    assert any(
        w["module"].startswith("src.services.idempotency.")
        and "UnprocessableEntityError" in w["message"]
        for w in warnings
    )

    # Verify ERROR payload (BookingService IntegrityError fallback)
    errors = [p for p in captured_payloads if p["level"] == "ERROR"]
    assert len(errors) == 1
    assert "Database IntegrityError fallback triggered on slot s1" in errors[0]["message"]
    assert (
        errors[0]["module"]
        == "src.services.booking_service.create_booking_with_pessimistic_lock"
    )
    assert "IntegrityError" in errors[0]["stack_trace"]

    # Verify CRITICAL payload (SlotService Redis ConnectionError halt)
    criticals = [p for p in captured_payloads if p["level"] == "CRITICAL"]
    assert len(criticals) == 1
    assert "ConnectionError" in criticals[0]["message"]
    assert criticals[0]["module"] == "src.services.slot_service.reset_all"
    assert "await self.redis.flushdb()" in criticals[0]["stack_trace"]
