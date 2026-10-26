"""API tests: real HTTP requests through FastAPI's TestClient to an in-memory DB."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from customer_pipeline.api import create_app
from customer_pipeline.config import Settings
from customer_pipeline.database import Customer, PipelineRun, utcnow

API_KEY = "test-key-123"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def settings(crm_csv, billing_xml):
    return replace(
        Settings.from_env(),
        database_url="sqlite://",
        crm_csv_path=crm_csv,
        billing_xml_path=billing_xml,
        api_key=API_KEY,
        log_file=None,
    )


@pytest.fixture
def client(settings, engine):
    with TestClient(create_app(settings, engine=engine)) as c:
        c.headers.update(AUTH)
        yield c


@pytest.fixture
def loaded_client(client, pipeline):
    pipeline.run()  # load sample data with the fixed-date validator
    return client


# --- health and metrics -----------------------------------------------------

def test_liveness(client):
    r = client.get("/health/live", headers={"X-API-Key": ""})
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_readiness_ok(client):
    assert client.get("/health/ready").json() == {"status": "ready", "database": "ok"}


def test_readiness_503_when_database_is_down(client, engine, monkeypatch):
    def broken_connect(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(engine, "connect", broken_connect)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not ready"
    assert client.get("/health/live").status_code == 200  # alive, just not ready


def test_metrics_use_route_templates_not_raw_ids(loaded_client):
    loaded_client.get("/customers/1")
    loaded_client.get("/customers/2")
    body = loaded_client.get("/metrics").text
    assert 'path="/customers/{customer_id}"' in body
    assert 'path="/customers/1"' not in body
    assert "pipeline_runs_total" in body


# --- authentication ---------------------------------------------------------

@pytest.mark.parametrize("path", ["/customers", "/customers/1", "/stats",
                                  "/pipeline/runs", "/pipeline/runs/1/rejects"])
def test_data_endpoints_need_api_key(client, path):
    r = client.get(path, headers={"X-API-Key": ""})
    assert r.status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_starting_a_run_needs_api_key(client):
    assert client.post("/pipeline/run", headers={"X-API-Key": "wrong"}).status_code == 401


def test_no_api_key_configured_means_open_dev_mode(settings, engine):
    with TestClient(create_app(replace(settings, api_key=None), engine=engine)) as c:
        assert c.get("/customers").status_code == 200


# --- security headers, request ids, CORS ------------------------------------

def test_security_headers(client):
    h = client.get("/customers").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Cache-Control"] == "no-store"
    assert "default-src 'none'" in h["Content-Security-Policy"]


def test_docs_page_still_loads(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "Content-Security-Policy" not in r.headers


def test_request_id_is_generated_or_passed_through(client):
    generated = client.get("/health/live").headers["X-Request-ID"]
    assert len(generated) == 16
    assert client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers[
        "X-Request-ID"] == "abc-123"
    # Unsafe ids (log injection attempts) are replaced, not echoed.
    bad = client.get("/health/live", headers={"X-Request-ID": "x\nFAKE LOG LINE"})
    assert "\n" not in bad.headers["X-Request-ID"]


def test_cors_only_for_configured_origins(settings, engine):
    app = create_app(replace(settings, cors_origins=("https://dashboard.example.com",)),
                     engine=engine)
    with TestClient(app) as c:
        preflight = {"Access-Control-Request-Method": "GET"}
        ok = c.options("/customers", headers={"Origin": "https://dashboard.example.com", **preflight})
        evil = c.options("/customers", headers={"Origin": "https://evil.example", **preflight})
    assert ok.headers["access-control-allow-origin"] == "https://dashboard.example.com"
    assert "access-control-allow-origin" not in evil.headers


def test_no_cors_headers_by_default(client):
    r = client.get("/customers", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# --- customers ----------------------------------------------------------------

def test_empty_database_lists_nothing(client):
    assert client.get("/customers").json() == {"total": 0, "limit": 20, "offset": 0, "items": []}


def test_list_customers(loaded_client):
    body = loaded_client.get("/customers").json()
    assert body["total"] == 10
    assert len(body["items"]) == 10


def test_pagination(loaded_client):
    page1 = loaded_client.get("/customers?limit=4&offset=0").json()
    page3 = loaded_client.get("/customers?limit=4&offset=8").json()
    assert page1["total"] == page3["total"] == 10
    assert len(page1["items"]) == 4
    assert len(page3["items"]) == 2


@pytest.mark.parametrize(
    "query, expected_total",
    [
        ("search=menon", 1),
        ("search=EXAMPLE.COM", 7),      # search is case-insensitive
        ("country=US", 1),
        ("country=in", 9),
        ("plan=pro", 4),
        ("plan=Enterprise", 2),
        ("source=billing", 8),    # customers that appear in billing
        ("source=crm&plan=Enterprise", 1),
    ],
)
def test_filters(loaded_client, query, expected_total):
    assert loaded_client.get(f"/customers?{query}").json()["total"] == expected_total


@pytest.mark.parametrize("term", ["%", "_", "' OR '1'='1", "'; DROP TABLE customers; --"])
def test_search_treats_wildcards_and_sql_as_plain_text(loaded_client, engine, term):
    assert loaded_client.get("/customers", params={"search": term}).json()["total"] == 0
    assert loaded_client.get("/customers").json()["total"] == 10  # table still intact


def test_get_customer_detail_shows_sources_and_version(loaded_client):
    first_id = loaded_client.get("/customers?search=ananya").json()["items"][0]["id"]
    body = loaded_client.get(f"/customers/{first_id}").json()
    assert body["email"] == "ananya.menon@example.com"
    assert {s["source_id"] for s in body["sources"]} == {"C001", "C009", "B-1001"}
    assert body["version"] == 1


def test_unknown_customer_is_404(client):
    r = client.get("/customers/9999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "country=IND",
                                   "source=salesforce", "search=" + "x" * 101])
def test_bad_query_params_are_422(client, query):
    assert client.get(f"/customers?{query}").status_code == 422


def test_stats(loaded_client):
    body = loaded_client.get("/stats").json()
    assert body["total_customers"] == 10
    assert body["in_multiple_sources"] == 6
    assert body["by_country"] == {"IN": 9, "US": 1}


# --- pipeline runs ------------------------------------------------------------

def test_run_is_accepted_then_finishes_in_background(client):
    r = client.post("/pipeline/run")
    assert r.status_code == 202
    run = r.json()
    assert run["status"] == "running"
    assert r.headers["Location"] == f"/pipeline/runs/{run['id']}"

    # TestClient runs background tasks before returning, so it's done now.
    done = client.get(f"/pipeline/runs/{run['id']}").json()
    assert done["status"] == "success"
    assert done["records_read"] == 21

    assert [x["id"] for x in client.get("/pipeline/runs").json()] == [run["id"]]
    ids = {x["source_id"] for x in client.get(f"/pipeline/runs/{run['id']}/rejects").json()}
    assert {"C004", "C006", "B-1006"} <= ids


def test_second_run_while_one_is_running_gets_409(client, session_factory):
    with session_factory() as s:
        s.add(PipelineRun(status="running"))
        s.commit()
    r = client.post("/pipeline/run")
    assert r.status_code == 409
    assert "already in progress" in r.json()["detail"]


def test_stale_running_run_is_expired_and_new_run_proceeds(client, session_factory):
    with session_factory() as s:
        s.add(PipelineRun(status="running", started_at=utcnow() - timedelta(hours=2)))
        s.commit()
    r = client.post("/pipeline/run")
    assert r.status_code == 202
    with session_factory() as s:
        old = s.scalar(select(PipelineRun).order_by(PipelineRun.id))
        assert old.status == "failed"
        assert "abandoned" in old.error


def test_unknown_run_is_404(client):
    assert client.get("/pipeline/runs/42").status_code == 404
    assert client.get("/pipeline/runs/42/rejects").status_code == 404


def test_missing_input_file_is_rejected_before_starting(settings, engine, tmp_path):
    app = create_app(replace(settings, billing_xml_path=tmp_path / "nope.xml"), engine=engine)
    with TestClient(app, headers=AUTH) as c:
        r = c.post("/pipeline/run")
        assert r.status_code == 422
        assert c.get("/pipeline/runs").json() == []


def test_broken_input_file_marks_run_failed(settings, engine, tmp_path, session_factory):
    broken = tmp_path / "broken.xml"
    broken.write_text("not xml at all")
    app = create_app(replace(settings, billing_xml_path=broken), engine=engine)
    with TestClient(app, headers=AUTH) as c:
        run_id = c.post("/pipeline/run").json()["id"]
        run = c.get(f"/pipeline/runs/{run_id}").json()
        assert run["status"] == "failed"
        assert "invalid XML" in run["error"]
        # The lock was released: the next run can start.
        assert c.post("/pipeline/run").status_code == 202
    with session_factory() as s:
        assert s.scalar(select(Customer)) is None  # nothing half-loaded


def test_metrics_report_pipeline_health_from_the_database(client, pipeline):
    pipeline.run()  # done outside the API process's request path, like the CronJob
    body = client.get("/metrics").text
    line = next(l for l in body.splitlines()
                if l.startswith("pipeline_last_success_timestamp_seconds "))
    import time
    assert abs(float(line.split()[1]) - time.time()) < 60  # correct UTC, not shifted by local TZ
    assert "pipeline_last_run_failed 0.0" in body
    assert "pipeline_last_run_reject_ratio 0.19" in body  # 4 of 21


def test_openapi_declares_api_key_scheme_for_authorize_button(client):
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key"}
