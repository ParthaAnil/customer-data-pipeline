import json

import pytest

from customer_pipeline.cli import is_loopback, main


def test_cli_run_writes_db_report_and_log(tmp_path, crm_csv, billing_xml, monkeypatch, capsys):
    log_file = tmp_path / "logs" / "pipeline.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"

    exit_code = main(["run", "--csv", str(crm_csv), "--xml", str(billing_xml), "--db", db_url])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "success"
    assert report["records_read"] == 21
    assert (tmp_path / "cli.db").exists()
    log_text = log_file.read_text()
    assert "crm: read 12 records" in log_text
    assert "merged" in log_text and "success" in log_text


def test_cli_missing_input_file_exits_2(tmp_path, billing_xml, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FILE", "")
    exit_code = main([
        "run", "--csv", str(tmp_path / "missing.csv"), "--xml", str(billing_xml),
        "--db", f"sqlite:///{tmp_path / 'x.db'}",
    ])
    assert exit_code == 2
    assert "file not found" in capsys.readouterr().err


def test_cli_exits_3_when_another_run_is_in_progress(tmp_path, crm_csv, billing_xml,
                                                     monkeypatch, capsys):
    from customer_pipeline.database import (
        PipelineRun, init_db, make_engine, make_session_factory,
    )

    monkeypatch.setenv("LOG_FILE", "")
    db_url = f"sqlite:///{tmp_path / 'locked.db'}"
    engine = make_engine(db_url)
    init_db(engine)
    with make_session_factory(engine)() as s:
        s.add(PipelineRun(status="running"))
        s.commit()
    engine.dispose()

    exit_code = main(["run", "--csv", str(crm_csv), "--xml", str(billing_xml), "--db", db_url])
    assert exit_code == 3
    assert "already in progress" in capsys.readouterr().err


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("0.0.0.0", False), ("192.168.1.20", False), ("example.com", False),
])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_serve_refuses_public_bind_without_api_key(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.delenv("API_KEY", raising=False)
    assert main(["serve", "--host", "0.0.0.0", "--db", "sqlite://"]) == 1
    assert "Refusing to listen" in capsys.readouterr().err
