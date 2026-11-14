"""Integration tests require an explicitly configured disposable PostgreSQL database."""

import os

import psycopg
import pytest

from bist.activity_reports.store import init_activity_report_tables
from bist.companies.metadata_store import connect_metadata_db


@pytest.fixture
def pg(monkeypatch):
    test_dsn = os.environ.get("BIST_TEST_DSN")
    if not test_dsn:
        pytest.skip(
            "Set BIST_TEST_DSN to a disposable bist_finance database with the shared schema"
        )
    with psycopg.connect(test_dsn) as probe:
        test_target = (probe.info.host, probe.info.port, probe.info.dbname)
    production_dsn = os.environ.get("BIST_FINANCE_DSN") or os.environ.get(
        "DATABASE_URL"
    )
    if production_dsn:
        with psycopg.connect(production_dsn) as probe:
            assert test_target != (
                probe.info.host,
                probe.info.port,
                probe.info.dbname,
            ), "Tests must use a separate database"
    monkeypatch.setenv("BIST_FINANCE_DSN", test_dsn)
    with connect_metadata_db() as conn:
        init_activity_report_tables(conn)
        conn.execute("DELETE FROM bronze.activity_report_documents")
        conn.execute("DELETE FROM metadata.activity_report_disclosures")
        conn.execute("DELETE FROM ops.activity_report_index_searches")
        conn.execute(
            "DELETE FROM ops.data_quality_results WHERE run_id IN (SELECT run_id FROM ops.pipeline_runs WHERE pipeline_name LIKE 'activity_report_%')"
        )
        conn.execute(
            "DELETE FROM ops.pipeline_runs WHERE pipeline_name LIKE 'activity_report_%'"
        )
        conn.execute(
            "INSERT INTO metadata.companies (symbol,mkk_member_oid,kap_member_title) VALUES ('THYAO','test-thyao','Turkish Airlines'),('FROTO','test-froto','Ford Otosan') ON CONFLICT (symbol) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO metadata.company_symbol_aliases (company_id,alias_symbol) SELECT company_id,'OLDTHY' FROM metadata.companies WHERE symbol='THYAO' ON CONFLICT DO NOTHING"
        )
    with connect_metadata_db() as conn:
        yield conn
