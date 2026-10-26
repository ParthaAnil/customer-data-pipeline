# Customer Data Integration Pipeline

[![ci](https://github.com/ParthaAnil/customer-data-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/ParthaAnil/customer-data-pipeline/actions/workflows/tests.yml)

Consolidates customer records from a CRM export (CSV) and a billing export (XML) into a single de-duplicated PostgreSQL database and serves them through a REST API.

The two systems disagree in the usual ways: different field layouts, phone and date formats, casing, and the same person registered under different emails. The pipeline normalises each record, rejects invalid ones with reasons, matches duplicates across sources, and keeps one record per customer with links back to every source record it came from.

With the bundled sample data, 21 source records become 10 customers, and 4 records are rejected.

**Stack:** Python 3.10+, FastAPI, SQLAlchemy 2, PostgreSQL (SQLite for local development), pytest, Prometheus, Docker, Kubernetes, GitHub Actions.

## Architecture

```mermaid
flowchart LR
    CSV[CRM CSV] --> E1[CsvCustomerExtractor]
    XML[Billing XML] --> E2[XmlCustomerExtractor]
    E1 --> C[Cleaner]
    E2 --> C
    C --> V[Validator]
    V -- rejected --> R[(rejected_records)]
    V -- valid --> M[Merger]
    M --> L[Repository: batched upsert]
    L --> DB[(customers\ncustomer_sources\npipeline_runs)]
    DB --> API[FastAPI]
```

| Stage | Module | Responsibility |
|---|---|---|
| Extract | `extractors.py` | Map each source layout onto a common `CustomerRecord` |
| Clean | `cleaner.py` | Normalise emails, phones (E.164), countries (ISO 3166), dates, names |
| Validate | `validator.py` | Accept or reject; a rejected record keeps every reason |
| Merge | `merger.py` | Group duplicates by shared email or phone (union-find), choose field values by source priority |
| Load | `repository.py` | Idempotent batched upsert; the only module that issues SQL |
| Orchestrate | `pipeline.py` | Run lock, single transaction per run, run audit row |
| Serve | `api.py` | REST endpoints, API-key auth, request IDs, metrics |

Design decisions and trade-offs are documented in [docs/DESIGN.md](docs/DESIGN.md).

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                               # test suite (in-memory SQLite)
customer-pipeline run                # load the sample data into customers.db

export API_KEY=dev-key               # PowerShell: $env:API_KEY="dev-key"
customer-pipeline serve              # http://127.0.0.1:8000/docs, then "Authorize"
```

Without `API_KEY`, the server starts in development mode on `127.0.0.1` only; it refuses to bind to a public address unauthenticated.

### Docker Compose (PostgreSQL + API)

```bash
cp .env.example .env                 # set real values
docker compose up --build -d
docker compose run --rm pipeline     # run the pipeline once
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/customers
```

Kubernetes manifests (API deployment with probes, nightly CronJob) are in `k8s/`, and Prometheus alert rules are in `monitoring/`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///customers.db` | e.g. `postgresql+psycopg://user:pass@host/db` (install the `postgres` extra) |
| `API_KEY` | unset | Required as `X-API-Key` on data endpoints when set |
| `CORS_ORIGINS` | unset | Comma-separated browser origins |
| `CRM_CSV_PATH`, `BILLING_XML_PATH` | `data/...` | Input files |
| `LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE` | `INFO`, `text`, `logs/pipeline.log` | `LOG_FORMAT=json` for log collectors |
| `DB_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS` | `10`, `30000` | Connection and per-query limits |
| `STALE_RUN_MINUTES` | `30` | After this, a run left `running` by a crashed process releases the lock |

## API

| Method | Path | Auth | |
|---|---|---|---|
| GET | `/health/live`, `/health/ready` | | Liveness; readiness (503 when the database is unreachable) |
| GET | `/metrics` | | Prometheus metrics |
| GET | `/customers` | key | Filters: `search`, `country`, `plan`, `source`; paginated (`limit` ≤ 100) |
| GET | `/customers/{id}` | key | Customer with the source records merged into it |
| GET | `/stats` | key | Totals by country and plan |
| POST | `/pipeline/run` | key | Starts a run in the background: `202` with `Location`, or `409` if one is running |
| GET | `/pipeline/runs`, `/pipeline/runs/{id}` | key | Run history and status |
| GET | `/pipeline/runs/{id}/rejects` | key | Rejected records with reasons |

## Tests

```bash
pytest
pytest --cov=customer_pipeline

# against PostgreSQL (adds a 20-thread run-lock race test)
TEST_DATABASE_URL=postgresql+psycopg://postgres:pw@localhost:5432/customers_test pytest
```

CI runs the suite on SQLite (Python 3.10 and 3.12) and PostgreSQL 16, then builds the Docker image and smoke-tests it.

## Sample data

All records in `data/` are synthetic. Email addresses use the reserved `example.com`, `example.net` and `example.org` domains (RFC 2606), so none can belong to a real person.

## Known limitations

- Matching on any shared phone number can merge family members who share one; a production matcher would score candidate pairs instead.
- The schema is created with `create_all`; migrations (Alembic) would be needed before schema changes in production.
- A run holds all records in memory, which is fine up to hundreds of thousands of rows; larger inputs need chunked processing.
- `search` uses `LIKE '%term%'`, which cannot use a B-tree index; `pg_trgm` would be the fix at scale.

## License

MIT
