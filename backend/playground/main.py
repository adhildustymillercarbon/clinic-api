import concurrent.futures
import json
import os
import urllib.request

API_KEY = "02e2f635-489d-470c-a36c-0d68edc7a9c6"
BASE_URL = "http://localhost:8000"

def post_json(path: str, data: dict) -> int:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code

def main():
    # 1. Reset slot
    post_json("/reset", {"slots": [{"id": "s1", "provider": "Dr Ade", "start": "2027-06-15T09:00:00Z", "end": "2027-06-15T09:30:00Z"}]})

    # 2. Fire 10 parallel requests across multiple threads
    def book(i):
        return post_json("/bookings", {
            "slot_id": "s1",
            "name": f"User {i}",
            "email": f"user{i}@example.com",
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        status_codes = list(executor.map(book, range(10)))

    print("Status codes:", status_codes)
    print("201 count (duplicates):", status_codes.count(201))

if __name__ == "__main__":
    main()