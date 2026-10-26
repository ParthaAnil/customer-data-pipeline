# Design notes

The system is one stateless API service and one batch job sharing a PostgreSQL database. Most of the decisions below follow from that shape.

## Data flow and correctness

**Common record shape.** Each extractor maps its source layout onto `CustomerRecord`. Cleaning, validation and merging never see source formats, so a new source (JSON, a SOAP endpoint) is one new `BaseExtractor` subclass.

**Clean, then validate.** The cleaner only reformats; values it cannot parse become `None`. The validator only decides, and it runs every rule, so a rejected record lists all of its problems. Rejected records are stored with their original data in `rejected_records`, which serves as the pipeline's dead-letter store: nothing is silently dropped.

**Matching.** Records sharing an email or a phone belong to the same customer, and matching is transitive. Groups are built with union-find in near-linear time. The known weakness is over-merging when two people share a phone; scoring candidate pairs (email + name similarity) would be the next step.

**Survivorship.** Each field takes the first non-empty value in a per-field source priority. Names and contact details trust the CRM; `plan` trusts billing, since billing is the system that charges it. `customer_since` is the earliest date seen. Blank values never overwrite stored ones.

**Idempotent loads.** An existing customer is found through a previously linked source record (`crm:C001`) or, failing that, by email. Re-running on unchanged input updates nothing and bumps no versions. This is what makes retries safe: the CronJob's `backoffLimit` and manual reruns cannot duplicate data.

**One transaction per run.** A failure at any stage rolls back the whole load, and the failure is still written to `pipeline_runs`.

## Concurrency

**Run lock.** Two overlapping runs would insert the same customers and fail on the unique email. A partial unique index allows at most one `pipeline_runs` row with `status = 'running'`, so the database itself refuses a second run. This holds across threads, processes, API replicas and the CronJob. A test races 20 threads against PostgreSQL and expects exactly one winner. The API answers `409` and the CLI exits with code 3.

**Lock expiry.** A crashed process would hold the lock forever, so a run still `running` after `STALE_RUN_MINUTES` is marked failed before a new run starts.

**Optimistic locking on customers.** `customers.version` is SQLAlchemy's `version_id_col`: an update succeeds only if the row still has the version that was read. Today the run lock means there is a single writer, but this guards any future write path (for example an edit endpoint) against lost updates.

**Deadlocks.** With a single writer and MVCC reads, there are no competing lock orders. `statement_timeout` bounds any query regardless.

## Database

- Indexes: unique `email`, unique `(source, source_id)`, plus `phone`, `country`, `plan` and foreign keys.
- The `plan` filter normalises its input instead of applying `lower()` to the column, which would prevent index use.
- Loading prefetches links and customers in batched `IN` queries. A test asserts that the SELECT count is the same for 5 and for 200 customers (no N+1).
- Connection pool: 5 connections plus 10 overflow, `pool_pre_ping`, 30-minute recycle. PostgreSQL connections set a connect timeout and `statement_timeout`.
- Not used, with reasons: read replicas, sharding and partitioning are unnecessary at this size; `rejected_records` would be the first candidate for monthly partitioning. A single primary is strongly consistent, so there is no eventual-consistency handling to do.

## API

- The pipeline endpoint returns `202 Accepted` with a `Location` header and runs in the background, so long runs never hold an HTTP request open past proxy timeouts. Clients poll the run resource.
- Liveness does not touch the database (restarting the process would not fix a database outage); readiness does, and returns `503` so the load balancer stops routing to the instance.
- Page size is capped at 100, and all query parameters have length or range limits.

## Security

| Concern | Handling |
|---|---|
| Authentication | `X-API-Key` on every endpoint that returns personal data or starts work, compared with `secrets.compare_digest`. Health and metrics stay open for probes. `serve` refuses a non-loopback bind without a key. OAuth/OIDC would replace this if end users ever log in directly. |
| SQL injection | All queries use bound parameters. LIKE wildcards in search input are escaped. Covered by tests with injection strings. |
| XSS | JSON only, with `nosniff` and a `default-src 'none'` CSP. The validator rejects `<`, `>` and control characters in stored text fields. |
| CORS / CSRF | CORS is off unless origins are listed explicitly. There are no cookies, and a custom header cannot be sent by a cross-site form, so CSRF does not apply. |
| XML | `defusedxml` rejects entity declarations (XXE, billion laughs). |
| Secrets | Read from the environment only (`.env` locally, a Kubernetes Secret in the cluster) and excluded from `repr`; the database password is masked in logs. |
| Caching | `Cache-Control: no-store` on every response, because responses contain personal data. |
| Containers | Non-root user, read-only root filesystem, no privilege escalation, and no published database port in Compose. |
| Platform | TLS, WAF, DDoS mitigation and rate limiting belong to the ingress or gateway in front of the service. |

## Operations

- **Logs:** text or JSON. Every line carries a request ID, which is also returned as `X-Request-ID`; caller-supplied IDs are accepted only if they match a safe pattern.
- **Metrics:** request counts and latency labelled by route template (bounded cardinality), plus run counts. Pipeline health (last success time, last run failed, reject ratio) is read from the database at scrape time, because CronJob runs happen in a process Prometheus never scrapes.
- **SLOs** (`monitoring/alerts.yml`): 99.5% of requests non-5xx over 30 days, p95 latency under 300 ms, and a successful run at least every 26 hours. The error-rate alert fires at a 14× burn rate of the 0.5% error budget.
- **Startup:** waits for the database with capped exponential backoff and jitter.
- **Deployment:** rolling updates with `maxUnavailable: 0` and readiness gating; rollback with `kubectl rollout undo` to a pinned image tag. Schema changes must stay backward-compatible for rollbacks to be safe, which is why migrations are listed as future work.

## Deliberately not included

Message queues, pub/sub, sagas and distributed transactions: there is one service and one database, and input arrives as files on a schedule. If sources moved to live change events, a queue feeding the same clean, validate and merge stages would be the natural extension.

Circuit breakers: the service calls no downstream services. One would be needed once a live CRM API becomes a source.

WebSockets, server-sent events and CDN or edge caching: there is no browser client, and responses are private.

Feature flags and leader election: there is a single code path and a single scheduled runner, guarded by the run lock and `concurrencyPolicy: Forbid`.
