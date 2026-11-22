"""Unified-Agent Normalized Telemetry Contract (Task #480).

Provides provider-neutral, privacy-bounded models and events for agent/run/model/tool telemetry.
"""

from .fixtures import (
    FIXTURE_SCENARIOS,
    generate_fixture,
)
from .producers import (
    producer_gap_report,
)
from .projection import (
    build_exception_digest,
    build_telemetry_payload,
    build_waterfall,
    project_telemetry,
    telemetry_payload,
)
from .redaction import (
    BOUNDED_PAYLOAD_LIMIT,
    BOUNDED_STRING_LIMIT,
    redact_sensitive_content,
    sanitize_metadata,
)
from .schema import (
    CHANNEL_TELEMETRY_PREFIX,
    SpanKind,
    SpanOutcome,
    TelemetrySpan,
    UsageMetrics,
    validate_telemetry_event,
)

__all__ = [
    "CHANNEL_TELEMETRY_PREFIX",
    "SpanKind",
    "SpanOutcome",
    "TelemetrySpan",
    "UsageMetrics",
    "validate_telemetry_event",
    "BOUNDED_PAYLOAD_LIMIT",
    "BOUNDED_STRING_LIMIT",
    "redact_sensitive_content",
    "sanitize_metadata",
    "FIXTURE_SCENARIOS",
    "generate_fixture",
    "producer_gap_report",
    "project_telemetry",
    "build_waterfall",
    "build_exception_digest",
    "telemetry_payload",
    "build_telemetry_payload",
]
