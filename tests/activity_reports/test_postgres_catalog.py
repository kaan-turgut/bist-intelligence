from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import requests

from bist.activity_reports import fetcher
from bist.activity_reports.catalog import (
    catalog_connection,
    company_lookup,
    subtract_coverage,
    upsert_disclosure,
)

PDF = b"%PDF-1.4\nfixture\n%%EOF\n"


def item(disclosure_id=100, symbol="THYAO", link="https://example.invalid/report.pdf"):
    return {
        "disclosureIndex": disclosure_id,
        "stockCodes": symbol,
        "year": 2020,
        "period": 2,
        "publishDate": "01.08.2020 12:30:00",
        "subject": "Faaliyet Raporu",
        "downloadLink": link,
        "customSourceField": {"preserve": True},
    }


def index(**kwargs):
    return fetcher.index_activity_reports(
        symbols=["THYAO"],
        begin_date="2020-01-01",
        end_date="2020-12-31",
        request_sleep=0,
        **kwargs,
    )


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_catalog_round_trip_preserves_unmapped_issuers_and_good_links(pg):
    lookup = company_lookup(pg)
    raw = item(symbol="ANACM")
    upsert_disclosure(pg, raw, lookup)
    row = pg.execute("SELECT * FROM metadata.activity_report_disclosures").fetchone()
    assert row["source_payload"] == raw and row["company_id"] is None
    assert row["published_at"].utcoffset() == timedelta(hours=3)
    upsert_disclosure(pg, {**raw, "downloadLink": None}, lookup)
    assert count(pg, "metadata.activity_report_disclosures") == 1
    assert count(pg, "ops.activity_report_downloads") == 1
    assert (
        pg.execute(
            "SELECT download_url FROM metadata.activity_report_disclosures"
        ).fetchone()["download_url"]
        == raw["downloadLink"]
    )


def test_interval_subtraction_merges_overlaps_and_handles_gaps():
    start, end = date(2020, 1, 1), date(2020, 1, 31)
    covered = [
        (date(2019, 12, 1), date(2020, 1, 5)),
        (date(2020, 1, 10), date(2020, 1, 20)),
        (date(2020, 1, 12), date(2020, 1, 18)),
    ]
    assert subtract_coverage(start, end, covered) == [
        (date(2020, 1, 6), date(2020, 1, 9)),
        (date(2020, 1, 21), end),
    ]


def test_completed_empty_searches_skip_even_when_batching_changes(pg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda session, interval, oids, **kw: calls.append((interval, oids)) or [],
    )
    pg.commit()
    args = {
        "symbols": ["THYAO", "FROTO"],
        "begin_date": "2020-01-01",
        "end_date": "2020-12-31",
        "request_sleep": 0,
    }
    assert fetcher.index_activity_reports(**args, chunk_size=1) == 2
    assert fetcher.index_activity_reports(**args, chunk_size=100) == 0
    assert len(calls) == 2
    assert count(pg, "ops.activity_report_index_searches") == 2


def test_overlapping_dates_only_query_missing_ranges_and_refresh_is_explicit(
    pg, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda session, interval, oids, **kw: calls.append(interval) or [],
    )
    pg.commit()
    assert index() == 1
    assert (
        fetcher.index_activity_reports(
            symbols=["THYAO"],
            begin_date="2020-06-01",
            end_date="2021-02-28",
            request_sleep=0,
        )
        == 1
    )
    assert calls == [("2020-01-01", "2020-12-31"), ("2021-01-01", "2021-02-28")]
    assert index(refresh=True) == 1


def test_today_is_never_marked_complete(pg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda session, interval, oids, **kw: calls.append(interval) or [],
    )
    pg.commit()
    kwargs = {
        "symbols": ["THYAO"],
        "begin_date": "2026-11-01",
        "end_date": "2026-11-04",
        "today": date(2026, 11, 4),
        "request_sleep": 0,
    }
    fetcher.index_activity_reports(**kwargs)
    fetcher.index_activity_reports(**kwargs)
    assert calls == [("2026-11-01", "2026-11-04"), ("2026-11-04", "2026-11-04")]
    assert pg.execute(
        "SELECT end_date FROM ops.activity_report_index_searches"
    ).fetchone()["end_date"] == date(2026, 11, 3)
    pg.commit()
    fetcher.index_activity_reports(
        **{**kwargs, "end_date": "2026-11-05", "today": date(2026, 11, 5)}
    )
    assert calls[-1] == ("2026-11-04", "2026-11-05")


def test_imported_disclosure_presence_does_not_claim_search_coverage(pg, monkeypatch):
    upsert_disclosure(pg, item(), company_lookup(pg))
    pg.commit()
    calls = []
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda *a, **kw: calls.append(1) or [item()],
    )
    assert index() == 1
    assert calls == [1] and count(pg, "metadata.activity_report_disclosures") == 1


def test_search_failure_is_not_checkpointed(pg, monkeypatch):
    def fail(*a, **kw):
        raise requests.ConnectionError("KAP unavailable")

    monkeypatch.setattr(fetcher, "post_activity_report_search", fail)
    pg.commit()
    with pytest.raises(RuntimeError, match="searches failed"):
        index()
    assert count(pg, "ops.activity_report_index_searches") == 0
    assert (
        pg.execute(
            "SELECT status FROM ops.pipeline_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["status"]
        == "failed"
    )
    pg.commit()
    monkeypatch.setattr(fetcher, "post_activity_report_search", lambda *a, **kw: [])
    assert index() == 1


def test_catalog_rows_and_checkpoint_are_one_transaction(pg, monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda *a, **kw: [item(100), {**item(101), "period": 8}],
    )
    pg.commit()
    with pytest.raises(RuntimeError, match="searches failed"):
        index()
    assert count(pg, "metadata.activity_report_disclosures") == 0
    assert count(pg, "ops.activity_report_index_searches") == 0


def test_error_payload_cannot_be_cached_as_empty_search(pg, monkeypatch):
    monkeypatch.setattr(
        fetcher, "post_activity_report_search", lambda *a, **kw: {"error": "rate limit"}
    )
    pg.commit()
    with pytest.raises(RuntimeError, match="searches failed"):
        index()
    assert count(pg, "ops.activity_report_index_searches") == 0


def test_attachment_interruption_does_not_repeat_the_completed_search(pg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetcher,
        "post_activity_report_search",
        lambda *a, **kw: calls.append(1) or [item(link=None)],
    )

    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(fetcher, "get_activity_report_pdf_link", interrupt)
    pg.commit()
    with pytest.raises(KeyboardInterrupt):
        index()
    assert count(pg, "ops.activity_report_index_searches") == 1
    pg.commit()
    monkeypatch.setattr(
        fetcher,
        "get_activity_report_pdf_link",
        lambda *a, **kw: "https://example.invalid/recovered.pdf",
    )
    assert index() == 0
    assert len(calls) == 1
    assert (
        pg.execute("SELECT download_url FROM metadata.activity_report_disclosures")
        .fetchone()["download_url"]
        .endswith("recovered.pdf")
    )


def test_downloads_reconcile_disk_and_retry_deleted_files(pg, tmp_path, monkeypatch):
    upsert_disclosure(pg, item(), company_lookup(pg))
    pg.commit()
    path = tmp_path / "THYAO" / "THYAO_2020_2_100.pdf"
    path.parent.mkdir()
    path.write_bytes(PDF)
    calls = []

    def download(session, url, output, **kw):
        calls.append(url)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PDF)

    monkeypatch.setattr(fetcher, "download_pdf", download)
    assert fetcher.download_activity_reports(output_dir=tmp_path, request_sleep=0) == 0
    assert calls == []
    path.unlink()
    assert fetcher.download_activity_reports(output_dir=tmp_path, request_sleep=0) == 1
    assert len(calls) == 1
    row = pg.execute("SELECT * FROM ops.activity_report_downloads").fetchone()
    assert (
        row["status"] == "downloaded"
        and row["attempt_count"] == 1
        and row["file_size_bytes"] == len(PDF)
    )


def test_missing_link_retried_during_download_without_search(pg, tmp_path, monkeypatch):
    upsert_disclosure(pg, item(link=None), company_lookup(pg))
    pg.commit()
    monkeypatch.setattr(
        fetcher,
        "get_activity_report_pdf_link",
        lambda *a, **kw: "https://example.invalid/retry.pdf",
    )
    monkeypatch.setattr(
        fetcher, "request_with_backoff", lambda *a, **kw: SimpleNamespace(content=PDF)
    )
    assert fetcher.download_activity_reports(output_dir=tmp_path, request_sleep=0) == 1
    assert (
        pg.execute("SELECT download_url FROM metadata.activity_report_disclosures")
        .fetchone()["download_url"]
        .endswith("retry.pdf")
    )


def test_html_response_cannot_be_registered_as_pdf_and_can_retry(
    pg, tmp_path, monkeypatch
):
    upsert_disclosure(pg, item(), company_lookup(pg))
    pg.commit()
    monkeypatch.setattr(
        fetcher,
        "request_with_backoff",
        lambda *a, **kw: SimpleNamespace(content=b"<html>Rate limited</html>"),
    )
    with pytest.raises(RuntimeError, match="downloads failed"):
        fetcher.download_activity_reports(output_dir=tmp_path, request_sleep=0)
    row = pg.execute(
        "SELECT status,attempt_count FROM ops.activity_report_downloads"
    ).fetchone()
    assert row["status"] == "failed" and row["attempt_count"] == 1
    assert not list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("*.part"))
    pg.commit()
    monkeypatch.setattr(
        fetcher, "request_with_backoff", lambda *a, **kw: SimpleNamespace(content=PDF)
    )
    assert fetcher.download_activity_reports(output_dir=tmp_path, request_sleep=0) == 1
    assert (
        pg.execute(
            "SELECT attempt_count FROM ops.activity_report_downloads"
        ).fetchone()["attempt_count"]
        == 2
    )


def test_catalog_writer_lock_prevents_duplicate_requests(pg):
    pg.commit()
    with (
        catalog_connection(),
        pytest.raises(RuntimeError, match="Another activity-report indexer"),
        catalog_connection(),
    ):
        pytest.fail("Concurrent catalog writers must be refused")
