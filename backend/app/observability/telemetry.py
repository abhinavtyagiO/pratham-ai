"""Safe structured application events, per-call tracing, and bounded aggregate metrics."""

import json
import logging
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from time import perf_counter

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger("business_pa")


class JSONFormatter(logging.Formatter):
    """Render application LogRecords with UTC timestamp, level, and structured safe payload.
    Call sites own redaction/allowlisting; this formatter is not a general secret scrubber
    and should not receive raw provider reprs.
    """

    def format(self, record):
        """Serialize a record's structured payload as Unicode JSON, converting non-JSON scalar
        types to strings. Arbitrary transcript/error objects must be excluded before this
        boundary.
        """
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                **record.payload,
            },
            ensure_ascii=False,
            default=str,
        )


def configure_logging(level: str = "INFO"):
    """Install one stderr JSON handler on the application logger, apply the requested level,
    and disable propagation. Repeated app/test construction avoids duplicate handlers;
    unrelated provider loggers are not configured here.
    """
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False


def event(name: str, call_id: str, trace_id: str, **safe_fields):
    # Call sites pass identifiers, bounded enums, counts, and hashes; never raw transcripts.
    """Emit an application event with call/trace correlation and caller-supplied safe metadata.
    This function does not inspect/redact arbitrary values; call sites must restrict fields
    to identifiers, counts, enums, hashes, or timings.
    """
    logger.info(
        name,
        extra={
            "payload": {
                "event": name,
                "call_id": call_id,
                "trace_id": trace_id,
                **safe_fields,
            }
        },
    )


class JSONSpanExporter(SpanExporter):
    """Export completed OpenTelemetry spans as structured application events. Holds no call-
    specific buffers; stage instrumentation disables raw exception capture so trace events
    remain metadata-only.
    """

    def export(self, spans):
        """Emit each completed span's identifiers, parent, status, timing, and safe attributes,
        returning SUCCESS. Transcript bodies and provider exception messages must never be
        span attributes.
        """
        for span in spans:
            attrs = dict(span.attributes or {})
            event(
                "trace_span",
                attrs.pop("call_id", None),
                f"{span.context.trace_id:032x}",
                span=span.name,
                span_id=f"{span.context.span_id:016x}",
                parent_span_id=f"{span.parent.span_id:016x}" if span.parent else None,
                latency_ms=(span.end_time - span.start_time) / 1_000_000,
                status=span.status.status_code.name,
                attributes=attrs,
            )
        return SpanExportResult.SUCCESS


COUNTERS = (
    "calls_started_total",
    "calls_completed_total",
    "calls_failed_total",
    "stage_errors_total",
    "database_errors_total",
    "resolved_calls",
    "needs_attention_calls",
    "rules_triggered_total",
    "unknown_intent_total",
    "model_failures_total",
    "model_tokens_total",
    "intents_detected_total",
)
HISTOGRAMS = (
    "stt_latency_ms",
    "intent_latency_ms",
    "policy_latency_ms",
    "llm_latency_ms",
    "tts_latency_ms",
    "end_to_end_turn_latency_ms",
    "voice_stage_latency_ms",
)


class Telemetry:
    """Own application/test OpenTelemetry tracer/meter providers and named instruments. Default
    exports safe spans to JSON and reads metrics in memory; an explicit OTLP endpoint adds
    background exporters/readers. Shared across calls with call IDs on spans, not mutable
    per-call fields; the creating app/CLI must shutdown it.
    """

    def __init__(self, otlp_endpoint: str | None = None, span_exporter=None):
        """Create service resources, tracer/meter providers, counters/histograms, and an in-
        memory metric reader. Accept an injected span exporter for tests; optional OTLP
        setup owns background exporters until shutdown().
        """
        resource = Resource.create({"service.name": "ai-business-pa", "service.version": "0.2.0"})
        self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        self.traces.add_span_processor(SimpleSpanProcessor(span_exporter or JSONSpanExporter()))
        self.reader = InMemoryMetricReader()
        readers = [self.reader]
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            base = otlp_endpoint.rstrip("/")
            self.traces.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
            )
            readers.append(
                PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"))
            )
        self.tracer = self.traces.get_tracer("business_pa.pipeline")
        self.meters = MeterProvider(
            resource=resource, metric_readers=readers, shutdown_on_exit=False
        )
        meter = self.meters.get_meter("business_pa.pipeline")
        self.counters = {name: meter.create_counter(name) for name in COUNTERS}
        self.histograms = {name: meter.create_histogram(name, unit="ms") for name in HISTOGRAMS}

    def count(self, name, **attrs):
        """Increment the named counter with bounded metric attributes. Avoid call IDs or
        transcript text in attributes because high-cardinality/customer data does not belong
        in aggregate metrics.
        """
        self.counters[name].add(1, attrs)

    @contextmanager
    def stage(self, name, call_id, metric=None):
        """Trace an operation, optionally recording histogram milliseconds on every exit. Yield
        the span, mark failures without recording sensitive exception text, and re-raise
        errors; caller cancellation still runs timing cleanup.
        """
        start = perf_counter()
        with self.tracer.start_as_current_span(
            name,
            attributes={"call_id": call_id},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except Exception:
                # Exception text can contain caller PII or provider secrets.
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                if metric:
                    self.histograms[metric].record((perf_counter() - start) * 1000)

    def metrics_snapshot(self):
        """Read current in-memory metrics as a JSON-compatible dictionary, returning empty data
        when no measurements exist. Used by the authenticated operator endpoint and tests.
        """
        data = self.reader.get_metrics_data()
        return json.loads(data.to_json()) if data else {}

    def shutdown(self):
        """Release owned tracer/meter providers and their optional exporter resources. The
        app/CLI invokes this once after its operations or sessions are finished.
        """
        self.traces.shutdown()
        self.meters.shutdown()
