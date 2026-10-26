"""Prometheus metrics exposed at GET /metrics.

Metric objects are module-level because prometheus_client keeps a single
global registry and rejects duplicate registrations.
"""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled",
    # `path` is the route template (/customers/{customer_id}), never the raw
    # URL, to keep label cardinality bounded.
    ["method", "path", "status"],
)

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Time spent handling HTTP requests", ["method", "path"]
)

PIPELINE_RUNS = Counter("pipeline_runs_total", "Pipeline runs by final status", ["status"])

PIPELINE_RECORDS = Counter(
    "pipeline_records_total", "Records processed, by outcome", ["outcome"]
)

PIPELINE_DURATION = Histogram(
    "pipeline_run_duration_seconds",
    "Wall time of a pipeline run",
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900),
)

# The counters above only see runs executed inside the API process. Scheduled
# runs execute in a short-lived CLI process that is never scraped, so pipeline
# health is also derived from the pipeline_runs table at scrape time.
LAST_SUCCESS_TIMESTAMP = Gauge(
    "pipeline_last_success_timestamp_seconds",
    "Unix time the last successful run finished (0 if never)",
)
LAST_RUN_FAILED = Gauge("pipeline_last_run_failed", "1 if the most recent finished run failed")
LAST_RUN_REJECT_RATIO = Gauge(
    "pipeline_last_run_reject_ratio", "Rejected / read records in the most recent finished run"
)
