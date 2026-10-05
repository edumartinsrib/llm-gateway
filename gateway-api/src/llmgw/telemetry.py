"""OpenTelemetry (mesmo arranjo do Orquestrador).

Usa as variáveis padrão do OTel: OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME e
OTEL_RESOURCE_ATTRIBUTES. Sem endpoint, nada é exportado e os logs continuam no
stdout com trace_id para correlação.
"""

from __future__ import annotations

import logging
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource

_configured = False


class _TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True


def setup_telemetry(service_name: str) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    service_name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None

    root = logging.getLogger()
    root.setLevel(os.environ.get("LLMGW_LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler(sys.stdout)
    stream.addFilter(_TraceContextFilter())
    stream.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [trace=%(trace_id)s] %(message)s")
    )
    root.handlers = [stream]
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
