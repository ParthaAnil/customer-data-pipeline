"""REST API over the integrated customer data.

Health and metrics endpoints are public. Everything else returns personal
data or starts work and requires the ``X-API-Key`` header when API_KEY is set.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from typing import Literal

from fastapi import (
    APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response, Security,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import __version__, metrics
from .config import Settings
from .database import init_db, make_engine, make_session_factory, wait_for_db
from .extractors import CsvCustomerExtractor, XmlCustomerExtractor
from .logging_config import configure_logging, request_id_var
from .pipeline import CustomerPipeline
from .repository import CustomerRepository, RunAlreadyInProgress
from .schemas import (
    CustomerDetailOut, CustomerOut, CustomerPage, RejectOut, RunOut, StatsOut,
)

logger = logging.getLogger(__name__)

# Caller-supplied request ids are echoed only if they look like ids,
# otherwise a header could inject fake lines into the logs.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",  # responses contain personal data
}
# JSON-only API; Swagger UI (/docs) loads assets from a CDN and is exempt.
CSP = "default-src 'none'; frame-ancestors 'none'"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_engine = engine is None
    engine = engine or make_engine(
        settings.database_url, settings.db_timeout_seconds, settings.db_statement_timeout_ms
    )
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        wait_for_db(engine)
        init_db(engine)
        if not settings.api_key:
            logger.warning("API_KEY is not set: data endpoints are unauthenticated (dev mode)")
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Customer Data Integration API",
        version=__version__,
        description="Merged customer records from the CRM (CSV) and billing (XML) systems.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def observe(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            path = route.path if route is not None else "unmatched"
            metrics.HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            metrics.HTTP_LATENCY.labels(request.method, path).observe(elapsed)
            logger.info("%s %s -> %d (%.1f ms)", request.method, request.url.path,
                        status, elapsed * 1000)
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith(DOCS_PATHS):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
            allow_credentials=False,
        )

    def require_api_key(key: str | None = Security(api_key_header)) -> None:
        if not settings.api_key:
            return
        if key is None or not secrets.compare_digest(key, settings.api_key):
            raise HTTPException(status_code=401, detail="invalid or missing API key",
                                headers={"WWW-Authenticate": "ApiKey"})

    def get_repo() -> Iterator[CustomerRepository]:
        with session_factory() as session:
            yield CustomerRepository(session)

    # -- public -----------------------------------------------------------

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict:
        # Deliberately independent of the database: restarting the process
        # would not fix a database outage.
        return {"status": "alive", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def readiness(response: Response) -> dict:
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("readiness check failed: %s", exc.__class__.__name__)
            response.status_code = 503
            return {"status": "not ready", "database": "unreachable"}
        return {"status": "ready", "database": "ok"}

    @app.get("/metrics", tags=["health"], include_in_schema=False)
    def prometheus_metrics() -> Response:
        try:
            with session_factory() as session:
                repo = CustomerRepository(session)
                last_ok = repo.last_finished_run("success")
                last = repo.last_finished_run()
            finished = last_ok.finished_at if last_ok else None
            if finished is not None and finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; stored as UTC
            metrics.LAST_SUCCESS_TIMESTAMP.set(finished.timestamp() if finished else 0)
            metrics.LAST_RUN_FAILED.set(1 if last and last.status == "failed" else 0)
            metrics.LAST_RUN_REJECT_RATIO.set(
                last.records_rejected / last.records_read if last and last.records_read else 0)
        except Exception as exc:
            logger.warning("could not refresh pipeline gauges: %s", exc.__class__.__name__)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # -- protected --------------------------------------------------------

    api = APIRouter(dependencies=[Depends(require_api_key)])

    @api.get("/customers", response_model=CustomerPage, tags=["customers"])
    def list_customers(
        search: str | None = Query(None, max_length=100, description="Matches name or email"),
        country: str | None = Query(None, min_length=2, max_length=2, examples=["IN"]),
        plan: str | None = Query(None, max_length=30, examples=["Pro"]),
        source: Literal["crm", "billing"] | None = Query(
            None, description="Only customers present in this source"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100_000),
        repo: CustomerRepository = Depends(get_repo),
    ) -> CustomerPage:
        rows, total = repo.list_customers(search, country, plan, source, limit, offset)
        return CustomerPage(
            total=total, limit=limit, offset=offset,
            items=[CustomerOut.model_validate(r) for r in rows],
        )

    @api.get("/customers/{customer_id}", response_model=CustomerDetailOut, tags=["customers"])
    def get_customer(customer_id: int, repo: CustomerRepository = Depends(get_repo)):
        customer = repo.get_customer(customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail=f"customer {customer_id} not found")
        return customer

    @api.get("/stats", response_model=StatsOut, tags=["customers"])
    def stats(repo: CustomerRepository = Depends(get_repo)):
        return repo.stats()

    @api.post("/pipeline/run", response_model=RunOut, status_code=202, tags=["pipeline"])
    def run_pipeline(
        background: BackgroundTasks, response: Response,
        repo: CustomerRepository = Depends(get_repo),
    ):
        """Start a run and return immediately; poll the URL in the Location header."""
        for path in (settings.crm_csv_path, settings.billing_xml_path):
            if not path.exists():
                raise HTTPException(status_code=422, detail=f"input file not found: {path.name}")

        pipeline = CustomerPipeline(
            extractors=[
                CsvCustomerExtractor(settings.crm_csv_path),
                XmlCustomerExtractor(settings.billing_xml_path),
            ],
            session_factory=session_factory,
            stale_after=timedelta(minutes=settings.stale_run_minutes),
        )
        try:
            run_id = pipeline.start()
        except RunAlreadyInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        background.add_task(pipeline.execute_in_background, run_id)
        response.headers["Location"] = f"/pipeline/runs/{run_id}"
        return repo.get_run(run_id)

    @api.get("/pipeline/runs", response_model=list[RunOut], tags=["pipeline"])
    def list_runs(
        limit: int = Query(20, ge=1, le=100), repo: CustomerRepository = Depends(get_repo)
    ):
        return repo.list_runs(limit)

    @api.get("/pipeline/runs/{run_id}", response_model=RunOut, tags=["pipeline"])
    def get_run(run_id: int, repo: CustomerRepository = Depends(get_repo)):
        run = repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return run

    @api.get("/pipeline/runs/{run_id}/rejects", response_model=list[RejectOut], tags=["pipeline"])
    def run_rejects(run_id: int, repo: CustomerRepository = Depends(get_repo)):
        if repo.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return repo.rejects_for_run(run_id)

    app.include_router(api)
    return app


def build_default_app() -> FastAPI:
    """Factory for ``uvicorn customer_pipeline.api:build_default_app --factory``."""
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_file, settings.log_format)
    return create_app(settings)
