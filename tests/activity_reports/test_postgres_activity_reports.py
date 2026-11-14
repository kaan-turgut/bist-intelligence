import pytest

from bist.activity_reports import processor
from bist.activity_reports.store import (
    fetch_pending_chunks,
    init_activity_report_tables,
    insert_extractions,
    pipeline_run,
    replace_chunks,
    store_embedding,
    upsert_chunk,
    upsert_document,
    writer_connection,
)
from bist.companies.loader import load_companies, resolve_symbols
from bist.companies.metadata_store import connect_metadata_db


def seed_chunk(conn, tmp_path, symbol="THYAO", disclosure_index=1, content=b"pdf"):
    path = tmp_path / f"{symbol}_2026_1_{disclosure_index}.pdf"
    path.write_bytes(content)
    document = upsert_document(conn, path, symbol, 2026, 1, disclosure_index)
    chunk = {
        "start_page": 3,
        "end_page": 8,
        "included_pages": [3, 8],
        "text": "[Page 3] Useful report text",
    }
    chunk_id = upsert_chunk(conn, document, chunk)
    return dict(chunk_id=chunk_id, document_id=document, **chunk)


def fact(page=3):
    return {
        "page": page,
        "category": "operations",
        "event_type": "expansion",
        "tags": ["fleet"],
        "sentiment": "positive",
        "magnitude": 4,
        "time_relevance": "future_outlook",
        "confidence": 0.9,
        "source_quote": "New planes",
        "info": "The company plans to expand its fleet.",
    }


def pending(conn, **kwargs):
    return fetch_pending_chunks(
        conn,
        model="test-model",
        extraction_version="v1",
        prompt_hash="prompt-1",
        **kwargs,
    )


def test_migration_is_idempotent_and_companies_are_shared(pg):
    assert init_activity_report_tables(pg) is False
    assert resolve_symbols(["oldthy", "THYAO"]) == ["THYAO"]
    assert load_companies()["THYAO"].oid == "test-thyao"
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM ops.schema_migrations WHERE version='activity_reports_001'"
        ).fetchone()["n"]
        == 1
    )


def test_documents_survive_moves_and_track_changed_content(pg, tmp_path):
    row = seed_chunk(pg, tmp_path)
    relocated = tmp_path / "relocated.pdf"
    relocated.write_bytes(b"pdf")
    assert upsert_document(pg, relocated, "OLDTHY", 2026, 1, 1) == row["document_id"]
    assert (
        seed_chunk(pg, tmp_path, content=b"revised pdf")["document_id"]
        != row["document_id"]
    )


def test_zero_facts_and_prompt_changes_control_pending(pg, tmp_path):
    row = seed_chunk(pg, tmp_path)
    insert_extractions(pg, row, {"Extractions": []}, "test-model", "v1", "prompt-1")
    assert pending(pg) == []
    assert (
        len(
            fetch_pending_chunks(
                pg, model="test-model", extraction_version="v1", prompt_hash="prompt-2"
            )
        )
        == 1
    )
    assert len(pending(pg, force=True)) == 1
    with pytest.raises(ValueError):
        pending(pg, symbols=["UNKNOWN"])


def test_latest_disclosure_is_pending_and_published(pg, tmp_path):
    older = seed_chunk(pg, tmp_path, disclosure_index=1)
    insert_extractions(
        pg, older, {"Extractions": [fact()]}, "test-model", "v1", "prompt-1"
    )
    newer = seed_chunk(pg, tmp_path, disclosure_index=2, content=b"newer")
    assert [r["chunk_id"] for r in pending(pg)] == [newer["chunk_id"]]
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM gold.current_activity_report_facts"
        ).fetchone()["n"]
        == 0
    )


def test_new_prompt_keeps_history_and_flags_invalid_page(pg, tmp_path):
    row = seed_chunk(pg, tmp_path)
    with pipeline_run(pg, "activity_report_test", {}) as run_id:
        insert_extractions(
            pg,
            row,
            {"Extractions": [fact()]},
            "test-model",
            "v1",
            "prompt-1",
            run_id=run_id,
        )
        insert_extractions(
            pg,
            row,
            {"Extractions": [fact(7)]},
            "test-model",
            "v1",
            "prompt-2",
            run_id=run_id,
        )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_extractions"
        ).fetchone()["n"]
        == 2
    )
    published = pg.execute(
        "SELECT * FROM gold.current_activity_report_facts"
    ).fetchall()
    assert len(published) == 1 and published[0]["page_in_chunk"] is False
    assert (
        pg.execute(
            "SELECT sum(affected_rows) AS n FROM ops.data_quality_results "
            "WHERE run_id=%s AND check_name='activity_report_page_in_chunk'",
            (run_id,),
        ).fetchone()["n"]
        == 1
    )
    # The fixture quote does not appear in the chunk text, so both facts are flagged.
    assert [r["quote_in_chunk"] for r in published] == [False]
    assert (
        pg.execute(
            "SELECT sum(affected_rows) AS n FROM ops.data_quality_results "
            "WHERE run_id=%s AND check_name='activity_report_quote_in_chunk'",
            (run_id,),
        ).fetchone()["n"]
        == 2
    )


def test_rechunking_keeps_facts_and_embeddings(pg, tmp_path):
    row = seed_chunk(pg, tmp_path)
    insert_extractions(
        pg, row, {"Extractions": [fact()]}, "test-model", "v1", "prompt-1"
    )
    store_embedding(pg, row["chunk_id"], "embed-model", [0.1, 0.2])
    replace_chunks(
        pg,
        row["document_id"],
        [
            {
                "start_page": 3,
                "end_page": 3,
                "included_pages": [3],
                "text": "Changed chunk",
            }
        ],
    )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_extractions"
        ).fetchone()["n"]
        == 1
    )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_embeddings"
        ).fetchone()["n"]
        == 1
    )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM gold.current_activity_report_facts"
        ).fetchone()["n"]
        == 0
    )


def test_failed_force_keeps_previous_completion(pg, tmp_path, monkeypatch):
    row = seed_chunk(pg, tmp_path)
    insert_extractions(
        pg,
        row,
        {"Extractions": [fact()]},
        "test-model",
        "v1",
        processor.extraction_prompt_hash(),
    )
    pg.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("API unavailable")

    monkeypatch.setattr(processor, "call_openai_extraction", fail)
    with pytest.raises(RuntimeError, match="API unavailable"):
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", force=True
        )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM gold.current_activity_report_facts"
        ).fetchone()["n"]
        == 1
    )
    assert (
        pg.execute(
            "SELECT status FROM ops.pipeline_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["status"]
        == "failed"
    )


def test_one_failed_chunk_does_not_discard_or_block_other_chunks(
    pg, tmp_path, monkeypatch
):
    seed_chunk(pg, tmp_path, symbol="FROTO", disclosure_index=2, content=b"a")
    seed_chunk(pg, tmp_path, symbol="THYAO", disclosure_index=1, content=b"b")
    pg.commit()

    def flaky(chunk_input, model):
        if "FROTO" in chunk_input:
            raise RuntimeError("transient failure")
        return {"Extractions": [fact()]}

    monkeypatch.setattr(processor, "call_openai_extraction", flaky)
    with pytest.raises(RuntimeError, match="1 of 2 chunks failed"):
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0
        )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM gold.current_activity_report_facts"
        ).fetchone()["n"]
        == 1
    )
    still_pending = fetch_pending_chunks(
        pg,
        model="test-model",
        extraction_version="v1",
        prompt_hash=processor.extraction_prompt_hash(),
    )
    assert [r["symbol"] for r in still_pending] == ["FROTO"]


def test_fatal_error_stops_the_run_before_wasting_more_requests(
    pg, tmp_path, monkeypatch
):
    for number, symbol in enumerate(("FROTO", "THYAO"), start=1):
        seed_chunk(
            pg,
            tmp_path,
            symbol=symbol,
            disclosure_index=number,
            content=b"x%d" % number,
        )
    pg.commit()
    calls = []

    def bad_key(chunk_input, model):
        calls.append(1)
        raise processor.OpenAIError("bad key", fatal=True)

    monkeypatch.setattr(processor, "call_openai_extraction", bad_key)
    with pytest.raises(RuntimeError, match="1 of 2 chunks failed and 1 were not"):
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0
        )
    assert len(calls) == 1
    still_pending = fetch_pending_chunks(
        pg,
        model="test-model",
        extraction_version="v1",
        prompt_hash=processor.extraction_prompt_hash(),
    )
    assert len(still_pending) == 2


def test_invalid_result_rolls_back_and_remains_pending(pg, tmp_path, monkeypatch):
    seed_chunk(pg, tmp_path)
    pg.commit()
    monkeypatch.setattr(
        processor,
        "call_openai_extraction",
        lambda *a, **kw: {"Extractions": [fact(), {"page": 3}]},
    )
    with pytest.raises(RuntimeError, match="1 of 1 chunks failed"):
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0
        )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_extractions"
        ).fetchone()["n"]
        == 0
    )
    assert (
        pg.execute("SELECT status FROM ops.activity_report_extraction_runs").fetchone()[
            "status"
        ]
        == "failed"
    )
    assert len(pending(pg)) == 1


def test_no_paid_call_for_completed_empty_results_and_embeddings_rerun(
    pg, tmp_path, monkeypatch
):
    row = seed_chunk(pg, tmp_path)
    insert_extractions(
        pg,
        row,
        {"Extractions": []},
        "test-model",
        "v1",
        processor.extraction_prompt_hash(),
    )
    pg.commit()

    def unexpected(*a, **kw):
        pytest.fail("A completed zero-fact chunk must not call the API")

    monkeypatch.setattr(processor, "call_openai_extraction", unexpected)
    assert (
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1"
        )
        == 0
    )
    monkeypatch.setattr(processor, "call_openai_embedding", lambda *a, **kw: [0.1, 0.2])
    assert (
        processor.embed_activity_report_chunks(model="test-embedding", request_sleep=0)
        == 1
    )
    assert (
        processor.embed_activity_report_chunks(model="test-embedding", request_sleep=0)
        == 0
    )


def test_writer_lock_prevents_duplicate_api_work(pg):
    pg.commit()
    with (
        writer_connection(),
        pytest.raises(RuntimeError, match="Another activity-report writer"),
        writer_connection(),
    ):
        pytest.fail("A concurrent writer must not start")


def test_wrong_database_is_rejected(pg, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    options = conninfo_to_dict(pg.info.dsn)
    options["dbname"] = "postgres"
    monkeypatch.setenv("BIST_FINANCE_DSN", make_conninfo(**options))
    with pytest.raises(RuntimeError, match="expected 'bist_finance'"):
        connect_metadata_db()


def test_ingest_and_extract_rerun_preserves_completed_results(
    pg, tmp_path, monkeypatch
):
    source = tmp_path / "THYAO_2026_1_1.pdf"
    source.write_bytes(b"test-pdf")
    monkeypatch.setattr(
        processor,
        "extract_pdf_pages",
        lambda *a, **kw: [
            {"page": 3, "text": "Fleet expansion details. " * 100, "char_count": 2400},
        ],
    )
    pg.commit()
    assert processor.ingest_activity_reports(symbols=["oldthy"], root_dir=tmp_path) == 1
    monkeypatch.setattr(
        processor, "call_openai_extraction", lambda *a, **kw: {"Extractions": [fact()]}
    )
    assert (
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0
        )
        == 1
    )
    before = pg.execute("SELECT id FROM silver.activity_report_extractions").fetchone()[
        "id"
    ]
    pg.commit()
    assert (
        processor.ingest_activity_reports(
            symbols=["THYAO"], root_dir=tmp_path, only_new=False
        )
        == 1
    )
    assert (
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0
        )
        == 0
    )
    assert (
        pg.execute("SELECT id FROM silver.activity_report_extractions").fetchone()["id"]
        == before
    )


def test_downloader_resolves_aliases_and_multi_symbol_issuers(
    pg, tmp_path, monkeypatch
):
    from bist.activity_reports import fetcher
    from bist.activity_reports.catalog import company_lookup, upsert_disclosure

    upsert_disclosure(
        pg,
        {
            "stockCodes": "OLDTHY, THYAO",
            "year": 2026,
            "period": 1,
            "disclosureIndex": 123,
            "downloadLink": "https://example.invalid/report.pdf",
        },
        company_lookup(pg),
    )
    pg.commit()
    body = b"%PDF-1.4\nfixture\n%%EOF\n"
    monkeypatch.setattr(
        fetcher,
        "download_pdf",
        lambda session, url, path, **kw: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.write_bytes(body),
        ),
    )
    fetcher.download_activity_reports(symbols=["oldthy"], output_dir=tmp_path)
    assert (tmp_path / "THYAO" / "THYAO_2026_1_123.pdf").read_bytes() == body


def test_clear_removes_only_the_requested_issuers_report_data(pg, tmp_path):
    first = seed_chunk(pg, tmp_path, symbol="THYAO")
    seed_chunk(pg, tmp_path, symbol="FROTO")
    insert_extractions(
        pg, first, {"Extractions": [fact()]}, "test-model", "v1", "prompt-1"
    )
    store_embedding(pg, first["chunk_id"], "test-embedding", [0.1, 0.2])
    company_count = pg.execute(
        "SELECT count(*) AS n FROM metadata.companies"
    ).fetchone()["n"]
    pg.commit()
    processor.clear_activity_report_data(symbols=["OLDTHY"])
    assert (
        pg.execute("SELECT count(*) AS n FROM metadata.companies").fetchone()["n"]
        == company_count
    )
    remaining = pg.execute(
        "SELECT c.symbol FROM bronze.activity_report_documents d JOIN metadata.companies c USING (company_id)"
    ).fetchall()
    assert [r["symbol"] for r in remaining] == ["FROTO"]
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_extractions"
        ).fetchone()["n"]
        == 0
    )
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM silver.activity_report_embeddings"
        ).fetchone()["n"]
        == 0
    )


def test_only_new_skips_ingested_pdfs_and_picks_up_new_or_changed_ones(
    pg, tmp_path, monkeypatch
):
    parsed = []

    def fake_parse(path, min_chars=500):
        parsed.append(path.name)
        return [
            {"page": 1, "text": "Fleet expansion details. " * 100, "char_count": 2500}
        ]

    monkeypatch.setattr(processor, "extract_pdf_pages", fake_parse)
    first = tmp_path / "THYAO_2026_1_1.pdf"
    first.write_bytes(b"one")
    pg.commit()
    assert processor.ingest_activity_reports(root_dir=tmp_path) == 1
    assert parsed == ["THYAO_2026_1_1.pdf"]

    # Nothing new: no PDF is parsed and nothing is processed.
    assert processor.ingest_activity_reports(root_dir=tmp_path) == 0
    assert parsed == ["THYAO_2026_1_1.pdf"]

    # A new report and a changed report are processed; the untouched one is not.
    (tmp_path / "FROTO_2026_1_2.pdf").write_bytes(b"two")
    first.write_bytes(b"one-revised")
    assert processor.ingest_activity_reports(root_dir=tmp_path) == 2
    assert sorted(parsed[1:]) == ["FROTO_2026_1_2.pdf", "THYAO_2026_1_1.pdf"]

    # The opt-out reprocesses everything.
    assert processor.ingest_activity_reports(root_dir=tmp_path, only_new=False) == 2


def test_concurrent_extraction_saves_every_chunk_once(pg, tmp_path, monkeypatch):
    first = seed_chunk(pg, tmp_path)
    for number in range(1, 6):
        upsert_chunk(
            pg,
            first["document_id"],
            {
                "start_page": 3,
                "end_page": 8,
                "included_pages": [3, 8],
                "text": f"[Page 3] Distinct chunk text {number}",
            },
        )
    pg.commit()
    seen = []

    def call(chunk_input, model):
        seen.append(chunk_input)
        return {"Extractions": [fact()]}

    monkeypatch.setattr(processor, "call_openai_extraction", call)
    assert (
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0, workers=4
        )
        == 6
    )
    assert len(seen) == len(set(seen)) == 6  # no chunk was sent twice
    assert (
        pg.execute(
            "SELECT count(*) AS n FROM gold.current_activity_report_facts"
        ).fetchone()["n"]
        == 6
    )
    # A second run has nothing left to pay for.
    monkeypatch.setattr(processor, "call_openai_extraction", lambda *a, **k: 1 / 0)
    assert (
        processor.extract_activity_report_facts(
            model="test-model", extraction_version="v1", request_sleep=0, workers=4
        )
        == 0
    )


def test_extraction_summary_counts_skipped_and_pending_without_loading_text(
    pg, tmp_path, monkeypatch
):
    first = seed_chunk(pg, tmp_path)
    for number in range(1, 4):
        upsert_chunk(
            pg,
            first["document_id"],
            {
                "start_page": 3,
                "end_page": 8,
                "included_pages": [3, 8],
                "text": f"[Page 3] Distinct chunk text {number}",
            },
        )
    prompt_hash = processor.extraction_prompt_hash()
    insert_extractions(
        pg, first, {"Extractions": [fact()]}, "test-model", "v1", prompt_hash
    )
    pg.commit()

    rows, totals = processor.summarize_extraction(["THYAO"], "test-model", "v1")
    assert [r["symbol"] for r in rows] == ["THYAO"]
    assert totals["active_chunks"] == 4
    assert totals["extracted_chunks"] == 1
    assert totals["pending_chunks"] == 3 and totals["pending_reports"] == 1
    assert totals["pending_chars"] > 0

    # A different model has nothing done; --force treats everything as pending.
    assert (
        processor.summarize_extraction(None, "other-model", "v1")[1]["pending_chunks"]
        == 4
    )
    assert (
        processor.summarize_extraction(None, "test-model", "v1", force=True)[1][
            "pending_chunks"
        ]
        == 4
    )

    # The summary agrees with what extraction would actually send.
    sent = []
    monkeypatch.setattr(
        processor,
        "call_openai_extraction",
        lambda chunk_input, model: sent.append(chunk_input) or {"Extractions": []},
    )
    processor.extract_activity_report_facts(
        model="test-model", extraction_version="v1", request_sleep=0
    )
    assert len(sent) == totals["pending_chunks"]


def new_fact(info, page=3, metrics=None):
    return {
        "page": page,
        "category": "operations",
        "event_type": "expansion",
        "themes": ["capacity"],
        "entities": ["AJet"],
        "metrics": metrics or [],
        "sentiment": "positive",
        "magnitude": 6,
        "time_relevance": "current_quarter",
        "source_quote": "Useful report text",
        "info": info,
    }


def seed_period_chunk(conn, tmp_path, year, period, disclosure_index):
    path = tmp_path / f"THYAO_{year}_{period}_{disclosure_index}.pdf"
    path.write_bytes(f"pdf {year} {period}".encode())
    document = upsert_document(conn, path, "THYAO", year, period, disclosure_index)
    chunk = {
        "start_page": 3,
        "end_page": 3,
        "included_pages": [3],
        "text": "Page 3\nUseful report text",
    }
    return dict(chunk_id=upsert_chunk(conn, document, chunk), **chunk)


def test_new_fields_and_metrics_reach_the_gold_views(pg, tmp_path):
    row = seed_period_chunk(pg, tmp_path, 2025, 4, 10)
    metric = {"name": "fleet_size", "value": 492, "unit": "aircraft", "period": "2025"}
    insert_extractions(
        pg,
        row,
        {"Extractions": [new_fact("Fleet reached 492 aircraft.", metrics=[metric])]},
        "test-model",
        "v2",
        "prompt",
    )
    pg.commit()
    fact_row = pg.execute(
        "SELECT themes, entities, confidence, quote_in_chunk FROM gold.current_activity_report_facts"
    ).fetchone()
    assert fact_row["themes"] == ["capacity"] and fact_row["entities"] == ["AJet"]
    assert fact_row["confidence"] is None and fact_row["quote_in_chunk"] is True
    metrics = pg.execute(
        "SELECT symbol, fiscal_year, metric_name, value, unit, metric_period "
        "FROM gold.current_activity_report_metrics"
    ).fetchall()
    assert [dict(m) for m in metrics] == [
        {
            "symbol": "THYAO",
            "fiscal_year": 2025,
            "metric_name": "fleet_size",
            "value": 492,
            "unit": "aircraft",
            "metric_period": "2025",
        }
    ]


def test_analyze_facts_labels_novelty_and_embeds_each_fact_once(
    pg, tmp_path, monkeypatch
):
    from bist.activity_reports import fact_analysis

    q1 = seed_period_chunk(pg, tmp_path, 2025, 1, 1)
    q2 = seed_period_chunk(pg, tmp_path, 2025, 2, 2)
    insert_extractions(
        pg, q1, {"Extractions": [new_fact("fleet"), new_fact("cargo")]}, "m", "v", "p"
    )
    insert_extractions(
        pg,
        q2,
        {"Extractions": [new_fact("fleet"), new_fact("route"), new_fact("route")]},
        "m",
        "v",
        "p",
    )
    pg.commit()
    vectors = {"fleet": [1, 0, 0], "cargo": [0, 1, 0], "route": [0, 0, 1]}
    embedded = []

    def fake_embeddings(texts, model, dimensions):
        embedded.extend(texts)
        return [vectors[text] for text in texts]

    monkeypatch.setattr(fact_analysis, "call_fact_embeddings", fake_embeddings)
    counts, duplicates = fact_analysis.analyze_facts(
        model="m", extraction_version="v", request_sleep=0
    )
    assert counts == {"first_report": 2, "repeated": 1, "new": 2}
    assert duplicates == 1
    labels = [
        (r["fiscal_quarter"], r["info"], r["novelty"], r["duplicate_of"] is not None)
        for r in pg.execute(
            "SELECT fiscal_quarter, info, novelty, duplicate_of "
            "FROM gold.current_activity_report_facts ORDER BY id"
        )
    ]
    assert labels == [
        (1, "fleet", "first_report", False),
        (1, "cargo", "first_report", False),
        (2, "fleet", "repeated", False),
        (2, "route", "new", False),
        (2, "route", "new", True),
    ]
    # Second run: nothing is embedded again, labels are recomputed locally.
    embedded.clear()
    fact_analysis.analyze_facts(model="m", extraction_version="v", request_sleep=0)
    assert embedded == []


def test_quality_check_stores_verdicts_and_skips_checked_facts(
    pg, tmp_path, monkeypatch
):
    from bist.activity_reports import fact_analysis

    row = seed_period_chunk(pg, tmp_path, 2025, 1, 1)
    insert_extractions(
        pg, row, {"Extractions": [new_fact("good"), new_fact("bad")]}, "m", "v", "p"
    )
    pg.commit()
    judged = []

    def fake_judge(judge_input, model):
        judged.append(judge_input)
        bad = '"info": "bad"' in judge_input
        return {
            "supported": not bad,
            "score": 1 if bad else 5,
            "issues": "wrong" if bad else "",
        }

    monkeypatch.setattr(fact_analysis, "call_judge", fake_judge)
    summary, worst = fact_analysis.quality_check(
        model="m", extraction_version="v", judge_model="judge", sample_size=10
    )
    assert summary["checked"] == 2 and summary["serious_problems"] == 1
    assert float(summary["supported_pct"]) == 50.0
    assert [w["info"] for w in worst] == ["bad"]
    assert "source_text" in judged[0]
    judged.clear()
    summary, _ = fact_analysis.quality_check(
        model="m", extraction_version="v", judge_model="judge", sample_size=10
    )
    assert judged == [] and summary["checked"] == 0


def test_token_usage_is_recorded_and_years_filter_limits_the_work(
    pg, tmp_path, monkeypatch
):
    seed_period_chunk(pg, tmp_path, 2024, 4, 1)
    seed_period_chunk(pg, tmp_path, 2025, 4, 2)
    pg.commit()
    sent = []

    def fake(chunk_input, model):
        sent.append(chunk_input)
        return {
            "Extractions": [new_fact("x")],
            "_usage": {"prompt_tokens": 1200, "completion_tokens": 300},
        }

    monkeypatch.setattr(processor, "call_openai_extraction", fake)
    _, before = processor.summarize_extraction(None, "m", "v", years=[2025])
    assert before["pending_chunks"] == 1
    processor.extract_activity_report_facts(
        model="m", extraction_version="v", request_sleep=0, years=[2025]
    )
    assert len(sent) == 1 and '"year": 2025' in sent[0]
    run = pg.execute(
        "SELECT input_tokens, output_tokens FROM ops.activity_report_extraction_runs"
    ).fetchone()
    assert (run["input_tokens"], run["output_tokens"]) == (1200, 300)
    _, after = processor.summarize_extraction(None, "m", "v")
    assert after["pending_chunks"] == 1  # the 2024 report is still pending
    assert after["measured"]["runs"] == 1
    assert float(after["measured"]["input_tokens"]) == 1200
