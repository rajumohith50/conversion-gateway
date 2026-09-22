"""Prometheus metrics, exactly the set in design section 8.

Every counter is labelled by source and conversion_action so a client's
question ("why are my closed_won conversions from HubSpot not showing up")
maps to a label selection. Where a value is not known yet (a payload that
failed schema validation has no conversion action) the label is "unknown"
rather than omitted, so the series still exists and can be alerted on.

upload_latency_seconds is the one exception: a batch carries rows from
several sources, so it is labelled by outcome instead.

All metrics live on the default registry. The API mounts /metrics; the
worker and uploader processes serve their own registry on METRICS_PORT.
"""

from prometheus_client import Counter, Gauge, Histogram, start_http_server

_SOURCE_ACTION = ["source", "conversion_action"]

events_received_total = Counter(
    "events_received_total", "Webhook events that authenticated", _SOURCE_ACTION
)
events_rejected_total = Counter(
    "events_rejected_total",
    "Events rejected at schema validation or normalisation",
    [*_SOURCE_ACTION, "reason"],
)
events_suppressed_total = Counter(
    "events_suppressed_total", "Events not uploaded because of consent", [*_SOURCE_ACTION, "reason"]
)
conversions_uploaded_total = Counter(
    "conversions_uploaded_total", "Conversions accepted by the platform", _SOURCE_ACTION
)
conversions_dead_lettered_total = Counter(
    "conversions_dead_lettered_total",
    "Conversions that permanently failed upload",
    [*_SOURCE_ACTION, "failure_class"],
)
upload_latency_seconds = Histogram(
    "upload_latency_seconds",
    "Wall time of one upload request to the platform",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
dlq_depth = Gauge("dlq_depth", "Dead-lettered events not yet replayed", ["source"])
ingest_to_upload_seconds = Histogram(
    "ingest_to_upload_seconds",
    "Seconds from webhook receipt to platform acceptance",
    _SOURCE_ACTION,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)


def unknown(value: str | None) -> str:
    return value if value else "unknown"


def read_counter(counter: Counter, **labels: str) -> float:
    """Current value of one labelled series. For tests and debugging; the
    production read path is /metrics."""
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return float(sample.value)
    return 0.0


def read_gauge(gauge: Gauge, **labels: str) -> float:
    for metric in gauge.collect():
        for sample in metric.samples:
            if sample.labels == labels:
                return float(sample.value)
    return 0.0


def histogram_count(histogram: Histogram, **labels: str) -> float:
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count") and sample.labels == labels:
                return float(sample.value)
    return 0.0


def serve_metrics(port: int) -> None:
    """Expose the registry over HTTP from a non-web process."""
    start_http_server(port)
