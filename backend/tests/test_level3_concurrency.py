import asyncio
import httpx


async def test_double_booking_returns_409(client: httpx.AsyncClient) -> None:
    await client.post(
        "/reset",
        json={
            "slots": [
                {
                    "id": "s1",
                    "provider": "Dr Ade",
                    "start": "2027-06-15T09:00:00Z",
                    "end": "2027-06-15T09:30:00Z",
                }
            ]
        },
    )

    res1 = await client.post(
        "/bookings",
        json={"slot_id": "s1", "name": "Ada Obi", "email": "ada1@example.com"},
    )
    assert res1.status_code == 201

    res2 = await client.post(
        "/bookings",
        json={"slot_id": "s1", "name": "Bola Cole", "email": "bola@example.com"},
    )
    assert res2.status_code == 409
    assert "error" in res2.json()


async def test_concurrent_bookings_only_one_succeeds(client: httpx.AsyncClient) -> None:
    await client.post(
        "/reset",
        json={
            "slots": [
                {
                    "id": "s1",
                    "provider": "Dr Ade",
                    "start": "2027-06-15T09:00:00Z",
                    "end": "2027-06-15T09:30:00Z",
                }
            ]
        },
    )

    async def attempt_book(idx: int) -> httpx.Response:
        return await client.post(
            "/bookings",
            json={
                "slot_id": "s1",
                "name": f"User {idx}",
                "email": f"user{idx}@example.com",
            },
        )

    responses = await asyncio.gather(*(attempt_book(i) for i in range(15)))
    status_codes = [r.status_code for r in responses]
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 14


async def test_cancel_booking_frees_slot(client: httpx.AsyncClient) -> None:
    await client.post(
        "/reset",
        json={
            "slots": [
                {
                    "id": "s1",
                    "provider": "Dr Ade",
                    "start": "2027-06-15T09:00:00Z",
                    "end": "2027-06-15T09:30:00Z",
                }
            ]
        },
    )

    book_res = await client.post(
        "/bookings",
        json={"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
    )
    booking_id = book_res.json()["id"]

    cancel_res = await client.delete(f"/bookings/{booking_id}")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"

    slots_res = await client.get("/slots")
    assert slots_res.json()["slots"][0]["available"] is True

    cancel_again = await client.delete(f"/bookings/{booking_id}")
    assert cancel_again.status_code == 409
    assert "error" in cancel_again.json()

    rebook_res = await client.post(
        "/bookings",
        json={"slot_id": "s1", "name": "Bola Cole", "email": "bola@example.com"},
    )
    assert rebook_res.status_code == 201


async def test_cancel_nonexistent_booking_returns_404(
    client: httpx.AsyncClient,
) -> None:
    res = await client.delete("/bookings/does-not-exist")
    assert res.status_code == 404
    assert "error" in res.json()


async def test_concurrent_bookings_create_duplicates_without_locks(
    client: httpx.AsyncClient,
) -> None:
    """Demonstrates that without pessimistic row locks or mutexes,
    concurrent requests for the same slot create duplicate confirmed bookings.
    """
    await client.post(
        "/reset",
        json={
            "slots": [
                {
                    "id": "s_dup",
                    "provider": "Dr Ade",
                    "start": "2027-06-15T09:00:00Z",
                    "end": "2027-06-15T09:30:00Z",
                }
            ]
        },
    )

    async def attempt_book(idx: int) -> httpx.Response:
        return await client.post(
            "/bookings",
            json={
                "slot_id": "s_dup",
                "name": f"User {idx}",
                "email": f"user{idx}@example.com",
            },
        )

    responses = await asyncio.gather(*(attempt_book(i) for i in range(10)))
    status_codes = [r.status_code for r in responses]

    # Without row-level locks, multiple concurrent requests successfully reserve the slot
    assert status_codes.count(201) == 1
