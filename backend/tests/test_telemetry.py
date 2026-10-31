from __future__ import annotations

from src.core.telemetry import (
    build_auth_header,
    build_logs_url,
    build_traces_url,
    clean_base_url,
)


def test_build_auth_header_with_base64_user_pass() -> None:
    # "hello@example.com:secret" encoded to base64
    b64_val = "aGVsbG9AZXhhbXBsZS5jb206c2VjcmV0"
    header = build_auth_header(b64_val)
    assert header == f"Basic {b64_val}"


def test_build_auth_header_with_plaintext_user_pass() -> None:
    plaintext = "root@example.com:Complexpass#123"
    header = build_auth_header(plaintext)
    assert header.startswith("Basic ")
    # Decoded should match the original plaintext
    import base64

    encoded = header.split(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-8") == plaintext


def test_build_auth_header_prefixed() -> None:
    assert (
        build_auth_header("Basic dXNlcjpwYXNz")
        == "Basic dXNlcjpwYXNz"
    )
    assert (
        build_auth_header("Bearer my-org-token")
        == "Bearer my-org-token"
    )


def test_build_auth_header_raw_token() -> None:
    header = build_auth_header("some-raw-api-token")
    assert header == "Bearer some-raw-api-token"


def test_clean_base_url() -> None:
    assert clean_base_url("https://api.openobserve.ai/") == "https://api.openobserve.ai"
    assert (
        clean_base_url("https://api.openobserve.ai/api/default/v1/traces")
        == "https://api.openobserve.ai/api/default"
    )
    assert (
        clean_base_url("https://api.openobserve.ai/api/default/v1/logs")
        == "https://api.openobserve.ai/api/default"
    )


def test_build_traces_and_logs_url() -> None:
    cloud_url = "https://api.openobserve.ai"
    assert (
        build_traces_url(cloud_url, "default")
        == "https://api.openobserve.ai/api/default/v1/traces"
    )
    assert (
        build_logs_url(cloud_url, "default")
        == "https://api.openobserve.ai/api/default/v1/logs"
    )

    custom_org_url = "https://api.openobserve.ai/api/myorg"
    assert (
        build_traces_url(custom_org_url, "myorg")
        == "https://api.openobserve.ai/api/myorg/v1/traces"
    )
    assert (
        build_logs_url(custom_org_url, "myorg")
        == "https://api.openobserve.ai/api/myorg/v1/logs"
    )
