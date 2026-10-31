from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request

logger = logging.getLogger("clinic_booking")

# Global references for cleanup during lifespan shutdown
_tracer_provider: Optional[Any] = None
_logger_provider: Optional[Any] = None
_telemetry_initialized: bool = False

# Try importing OpenTelemetry components gracefully
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    OTEL_TRACE_AVAILABLE = True
except ImportError:
    OTEL_TRACE_AVAILABLE = False
    trace = None

try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    OTEL_LOGS_AVAILABLE = True
except ImportError:
    OTEL_LOGS_AVAILABLE = False


def build_auth_header(token: str) -> str:
    """Format and normalize the OpenObserve authorization header.

    Supports:
    - Pre-formatted headers: 'Basic ...' or 'Bearer ...'
    - Base64-encoded 'username:password' or 'email:token'
    - Raw strings with 'username:password' (which get base64 encoded)
    - Raw API tokens (prefixed with Bearer)
    """
    cleaned = token.strip()
    if cleaned.startswith("Basic ") or cleaned.startswith("Bearer "):
        return cleaned

    # Check if the token is already base64-encoded user:pass
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8")
        if ":" in decoded:
            return f"Basic {cleaned}"
    except Exception:
        pass

    # Check if the token is in plaintext user:pass format
    if ":" in cleaned:
        encoded = base64.b64encode(cleaned.encode("utf-8")).decode("utf-8")
        return f"Basic {encoded}"

    # Default fallback for ingestion tokens
    return f"Bearer {cleaned}"


def clean_base_url(endpoint: str) -> str:
    """Strip trailing slashes and common suffix paths from endpoint URL."""
    base = endpoint.rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base.rstrip("/")


def build_traces_url(endpoint: str, org: str) -> str:
    """Construct the OpenObserve OTLP traces endpoint."""
    base = clean_base_url(endpoint)
    if "/api/" in base:
        return f"{base}/v1/traces"
    return f"{base}/api/{org}/v1/traces"


def build_logs_url(endpoint: str, org: str) -> str:
    """Construct the OpenObserve OTLP logs endpoint."""
    base = clean_base_url(endpoint)
    if "/api/" in base:
        return f"{base}/v1/logs"
    return f"{base}/api/{org}/v1/logs"


def setup_telemetry(app: FastAPI) -> None:
    """Initialize OpenObserve tracing and logging integration."""
    global _tracer_provider, _logger_provider, _telemetry_initialized

    if _telemetry_initialized:
        return

    from src.core.config import get_settings

    settings = get_settings()

    # Configure base console logging so stdout is consistently formatted
    root_logger = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
        )
        root_logger.addHandler(console_handler)
    root_logger.setLevel(logging.INFO)

    if not settings.openobserve_enabled:
        logger.info("OpenObserve telemetry is explicitly disabled.")
        return

    token = settings.effective_openobserve_token
    if not token:
        logger.info(
            "OpenObserve token not found in environment; telemetry export is disabled."
        )
        return

    if not OTEL_TRACE_AVAILABLE:
        logger.warning(
            "OpenTelemetry libraries are not installed. "
            "Please install opentelemetry-api, opentelemetry-sdk, "
            "opentelemetry-exporter-otlp-proto-http, and "
            "opentelemetry-instrumentation-fastapi to enable OpenObserve export."
        )
        return

    auth_header = build_auth_header(token)
    headers = {"Authorization": auth_header}
    if settings.openobserve_stream:
        headers["stream-name"] = settings.openobserve_stream

    resource = Resource.create(
        {
            SERVICE_NAME: settings.openobserve_service_name,
            SERVICE_VERSION: settings.app_version,
            "organization": settings.openobserve_org,
        }
    )

    # 1. Configure OpenTelemetry Traces
    traces_url = build_traces_url(settings.openobserve_endpoint, settings.openobserve_org)
    try:
        trace_exporter = OTLPSpanExporter(endpoint=traces_url, headers=headers)
        span_processor = BatchSpanProcessor(trace_exporter)
        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(_tracer_provider)

        # Instrument FastAPI
        FastAPIInstrumentor.instrument_app(app, tracer_provider=_tracer_provider)
        logger.info("OpenObserve tracing enabled -> %s", traces_url)
    except Exception as exc:
        logger.error("Failed to initialize OpenObserve tracing: %s", exc)

    # 2. Configure OpenTelemetry Logs
    if OTEL_LOGS_AVAILABLE:
        logs_url = build_logs_url(settings.openobserve_endpoint, settings.openobserve_org)
        try:
            log_exporter = OTLPLogExporter(endpoint=logs_url, headers=headers)
            log_processor = BatchLogRecordProcessor(log_exporter)
            _logger_provider = LoggerProvider(resource=resource)
            _logger_provider.add_log_record_processor(log_processor)
            set_logger_provider(_logger_provider)

            # Route standard Python logging records to OpenObserve
            otel_log_handler = LoggingHandler(
                level=logging.INFO, logger_provider=_logger_provider
            )
            root_logger.addHandler(otel_log_handler)
            logger.info("OpenObserve logging enabled -> %s", logs_url)
        except Exception as exc:
            logger.error("Failed to initialize OpenObserve logging: %s", exc)
    else:
        logger.warning(
            "OpenTelemetry logging components not available. "
            "Trace-correlated logging to OpenObserve is disabled."
        )

    # 3. Add HTTP request audit logging middleware
    @app.middleware("http")
    async def request_telemetry_middleware(request: Any, call_next: Any) -> Any:
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        # Avoid polluting logs with swagger / openapi polling
        if not request.url.path.startswith(("/docs", "/openapi.json", "/favicon.ico")):
            logger.info(
                "%s %s %d - %.2fms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "http.method": request.method,
                    "http.url": str(request.url),
                    "http.status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
        return response

    _telemetry_initialized = True


def shutdown_telemetry() -> None:
    """Flush and shut down active telemetry exporters before application exit."""
    global _tracer_provider, _logger_provider, _telemetry_initialized

    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception as exc:
            logger.warning("Error shutting down OpenTelemetry tracer provider: %s", exc)
        _tracer_provider = None

    if _logger_provider is not None:
        try:
            _logger_provider.shutdown()
        except Exception as exc:
            logger.warning("Error shutting down OpenTelemetry logger provider: %s", exc)
        _logger_provider = None

    _telemetry_initialized = False


def get_tracer(name: str = "clinic_booking"):
    """Return an active tracer if OpenTelemetry is configured."""
    if OTEL_TRACE_AVAILABLE and trace is not None:
        return trace.get_tracer(name)
    return None
