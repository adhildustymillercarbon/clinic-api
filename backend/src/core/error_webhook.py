from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import traceback
from types import TracebackType
from typing import Any

from fastapi import FastAPI, Request
import httpx

# Internal logger with propagation disabled to prevent recursive logging loops
_internal_logger = logging.getLogger("fastapi_error_webhook.internal")
_internal_logger.propagate = False


def _read_dotenv_fallback(env_path: str = ".env") -> dict[str, str]:
    """Lightweight zero-dependency .env parser for portable standalone usage."""
    values: dict[str, str] = {}
    path = Path(env_path)
    if not path.is_file():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip("'\"")
    except Exception:
        pass
    return values


@dataclass(slots=True)
class WebhookSettings:
    """Portable configuration resolved from explicit kwargs, os.environ, or .env."""

    url: str | None = None
    bearer_token: str | None = None
    enabled: bool = True
    timeout_seconds: float = 5.0
    throttle_seconds: int = 60
    max_queue_size: int = 1000
    app_package_marker: str = "/src/"
    warning_status_codes: tuple[int, ...] = (401, 422, 429)

    @classmethod
    def from_env(cls, **overrides: Any) -> WebhookSettings:
        dotenv_vals = _read_dotenv_fallback()

        def _get(name: str, default: str | None = None) -> str | None:
            return os.getenv(name) or dotenv_vals.get(name, default)

        raw_enabled = (_get("ERROR_WEBHOOK_ENABLED", "true") or "true").strip().lower()
        raw_timeout = _get("ERROR_WEBHOOK_TIMEOUT", "5.0") or "5.0"
        raw_throttle = _get("ERROR_WEBHOOK_THROTTLE_SECONDS", "60") or "60"

        raw_url = _get("ERROR_WEBHOOK_URL") or None
        if (
            raw_url
            and Path("/.dockerenv").exists()
            and raw_url.startswith(("http://localhost:", "http://127.0.0.1:"))
        ):
            raw_url = raw_url.replace(
                "http://localhost:", "http://host.docker.internal:", 1
            ).replace("http://127.0.0.1:", "http://host.docker.internal:", 1)

        cfg = cls(
            url=raw_url,
            bearer_token=_get("ERROR_WEBHOOK_TOKEN") or None,
            enabled=raw_enabled in ("1", "true", "yes", "on"),
            timeout_seconds=float(raw_timeout),
            throttle_seconds=int(raw_throttle),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    @property
    def is_active(self) -> bool:
        return bool(self.enabled and self.url)

    def build_headers(self) -> dict[str, str]:
        """Construct HTTP headers with normalized Bearer token authorization."""
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            token = self.bearer_token.strip()
            if not token.lower().startswith("bearer "):
                token = f"Bearer {token}"
            else:
                token = f"Bearer {token[7:].strip()}"
            headers["Authorization"] = token
        return headers


class ErrorWebhookDispatcher:
    """Portable async queue + httpx transport with Bearer auth and deduplication."""

    def __init__(
        self,
        settings: WebhookSettings,
        redis_getter: Callable[[], Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.redis_getter = redis_getter
        self._transport = transport
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(
            maxsize=settings.max_queue_size
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._local_dedup: dict[str, float] = {}

    def extract_origin_module(
        self, tb: TracebackType | None, default: str = "unknown"
    ) -> str:
        """Walk traceback frames to locate the deepest application module and function."""
        if tb is None:
            return default
        frames = traceback.extract_tb(tb)
        marker = self.settings.app_package_marker
        for frame in reversed(frames):
            norm_file = frame.filename.replace("\\", "/")
            if marker in norm_file and not norm_file.endswith("/exceptions.py"):
                rel = norm_file.split(marker, 1)[1]
                prefix = marker.strip("/").replace("/", ".")
                mod_path = f"{prefix}.{rel.removesuffix('.py').replace('/', '.')}"
                return f"{mod_path}.{frame.name}"
        last = frames[-1] if frames else None
        return f"{last.filename}:{last.name}" if last else default

    def ensure_worker_started(self) -> None:
        """Lazily start the background worker on the active asyncio event loop."""
        if not self.settings.is_active:
            return
        if self._worker_task is None or self._worker_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._worker_task = loop.create_task(
                    self._worker_loop(), name="error_webhook_worker"
                )
            except RuntimeError:
                pass

    def enqueue(
        self,
        *,
        level: str,
        message: str,
        module: str,
        stack_trace: str,
    ) -> None:
        """Non-blocking synchronous enqueue (0ms network I/O on request path)."""
        if not self.settings.is_active:
            return
        self.ensure_worker_started()
        payload = {
            "level": level.upper(),
            "message": message,
            "module": module,
            "stack_trace": stack_trace,
        }
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    def capture_exception(
        self, exc: BaseException, *, level: str = "CRITICAL"
    ) -> None:
        """Extract origin module + formatted traceback from an exception and enqueue once."""
        if getattr(exc, "_webhook_reported", False):
            return
        try:
            setattr(exc, "_webhook_reported", True)
        except Exception:
            pass

        tb_str = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        module = self.extract_origin_module(
            exc.__traceback__, default=type(exc).__module__
        )
        msg = getattr(exc, "message", None) or str(exc)
        self.enqueue(
            level=level,
            message=f"{type(exc).__name__}: {msg}",
            module=module,
            stack_trace=tb_str,
        )

    async def _is_duplicate(self, payload: dict[str, str]) -> bool:
        """Deduplicate identical errors within throttle_seconds via Redis or in-memory fallback."""
        ttl = self.settings.throttle_seconds
        if ttl <= 0:
            return False

        raw_fp = f"{payload['level']}:{payload['module']}:{payload['message'][:120]}"
        fp_hash = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()[:16]

        # 1. Optional Redis SETNX deduplication across workers
        if self.redis_getter is not None:
            try:
                redis_client = self.redis_getter()
                if redis_client is not None:
                    acquired = await redis_client.set(
                        f"error_webhook:dedup:{fp_hash}", "1", ex=ttl, nx=True
                    )
                    return not bool(acquired)
            except Exception:
                pass  # Fall back to in-memory cache if Redis itself is down

        # 2. Portable in-memory TTL deduplication
        now = time.monotonic()
        expired = [k for k, exp in self._local_dedup.items() if exp <= now]
        for k in expired:
            self._local_dedup.pop(k, None)

        if fp_hash in self._local_dedup:
            return True
        self._local_dedup[fp_hash] = now + ttl
        return False

    async def _deliver_with_retry(
        self, client: httpx.AsyncClient, payload: dict[str, str]
    ) -> None:
        """POST JSON payload with Bearer token and exponential backoff on transient failures."""
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = self.settings.build_headers()

        for attempt in range(1, 4):
            try:
                resp = await client.post(
                    self.settings.url,  # type: ignore[arg-type]
                    content=body_bytes,
                    headers=headers,
                )
                if 200 <= resp.status_code < 300:
                    return
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return  # Permanent client/auth/contract error; do not retry
            except httpx.TransportError:
                pass

            if attempt < 3:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    async def _worker_loop(self) -> None:
        timeout = httpx.Timeout(self.settings.timeout_seconds, connect=2.0)
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(
            timeout=timeout, limits=limits, transport=self._transport
        ) as client:
            while True:
                payload = await self._queue.get()
                try:
                    if not await self._is_duplicate(payload):
                        await self._deliver_with_retry(client, payload)
                except Exception:
                    pass
                finally:
                    self._queue.task_done()

    async def flush(self, timeout: float = 3.0) -> None:
        """Wait for all currently queued items to be processed."""
        if self._queue.empty():
            return
        self.ensure_worker_started()
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def shutdown(self, drain_timeout: float = 3.0) -> None:
        """Drain remaining items and cancel the background worker task."""
        if self._worker_task is None:
            return
        try:
            await self.flush(timeout=drain_timeout)
        except (asyncio.TimeoutError, Exception):
            pass
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None


class _WebhookLoggingHandler(logging.Handler):
    """Standard logging handler capturing WARNING, ERROR, and CRITICAL records."""

    def __init__(self, dispatcher: ErrorWebhookDispatcher) -> None:
        super().__init__(level=logging.WARNING)
        self.dispatcher = dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(
            ("fastapi_error_webhook", "httpx", "httpcore", "opentelemetry")
        ):
            return
        try:
            tb_str = ""
            module = f"{record.name}.{record.funcName}"
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                if getattr(exc, "_webhook_reported", False):
                    return
                try:
                    setattr(exc, "_webhook_reported", True)
                except Exception:
                    pass
                tb_str = "".join(traceback.format_exception(*record.exc_info))
                if record.name == "root":
                    module = self.dispatcher.extract_origin_module(
                        record.exc_info[2], default=module
                    )
            elif record.stack_info:
                tb_str = record.stack_info

            self.dispatcher.enqueue(
                level=record.levelname,
                message=record.getMessage(),
                module=module,
                stack_trace=tb_str,
            )
        except Exception:
            pass


def classify_unhandled_exception(exc: BaseException) -> str:
    """Distinguish infrastructure/connectivity outages (CRITICAL) from application bugs (ERROR)."""
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    critical_names = {
        "ConnectionError",
        "ConnectError",
        "TimeoutError",
        "OSError",
        "OperationalError",
        "InterfaceError",
        "PostgresConnectionError",
        "CannotConnectNowError",
        "ConnectionDoesNotExistError",
    }
    if mro_names & critical_names:
        return "CRITICAL"
    return "ERROR"


def setup_error_webhook(
    app: FastAPI,
    *,
    url: str | None = None,
    bearer_token: str | None = None,
    redis_getter: Callable[[], Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **overrides: Any,
) -> ErrorWebhookDispatcher:
    """Plug-and-play error webhook integration for any FastAPI application."""
    settings = WebhookSettings.from_env(
        url=url, bearer_token=bearer_token, **overrides
    )
    dispatcher = ErrorWebhookDispatcher(
        settings, redis_getter=redis_getter, transport=transport
    )
    app.state.error_webhook = dispatcher

    # 1. Attach (or refresh) root logging handler for logger.warning / logger.error
    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        if isinstance(existing, _WebhookLoggingHandler):
            root_logger.removeHandler(existing)
    root_logger.addHandler(_WebhookLoggingHandler(dispatcher))

    # 2. Wrap FastAPI exception handlers to capture halted service exceptions
    orig_add_handler = app.add_exception_handler

    def _wrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
        orig_fn = getattr(handler, "_error_webhook_orig", handler)

        async def _wrapped(request: Request, exc: Exception) -> Any:
            active_dispatcher: ErrorWebhookDispatcher = getattr(
                request.app.state, "error_webhook", dispatcher
            )
            severity = getattr(exc, "severity", None)
            raw_status = getattr(exc, "status_code", None)
            status_code = int(raw_status) if isinstance(raw_status, int) else 500
            if severity:
                active_dispatcher.capture_exception(exc, level=str(severity))
            elif status_code >= 500:
                active_dispatcher.capture_exception(
                    exc, level=classify_unhandled_exception(exc)
                )
            elif status_code in active_dispatcher.settings.warning_status_codes:
                active_dispatcher.capture_exception(exc, level="WARNING")
            return await orig_fn(request, exc)

        setattr(_wrapped, "_error_webhook_orig", orig_fn)
        return _wrapped

    for key, existing_handler in list(app.exception_handlers.items()):
        app.exception_handlers[key] = _wrap_handler(existing_handler)

    def _intercepted_add_handler(
        exc_class_or_code: Any, handler: Callable[..., Any]
    ) -> None:
        orig_add_handler(exc_class_or_code, _wrap_handler(handler))

    app.add_exception_handler = _intercepted_add_handler  # type: ignore[method-assign]
    if app.middleware_stack is not None:
        app.middleware_stack = None

    app.router.on_shutdown.append(dispatcher.shutdown)
    return dispatcher
