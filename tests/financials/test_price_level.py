"""The project-wide price level: one CPI target month for every inflation-adjusted value."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bist.financials import cli
from bist.financials.services import postgres_financial_pipeline as pipeline
from bist.financials.services import postgres_price_pipeline as prices
from bist.financials.services import price_level as levels
from bist.financials.services.financial_report_context import store_context

TEST = [SimpleNamespace(symbol="TEST")]
# 1% monthly CPI in 2024: April-December compounds to 1.01**9 between 2024-03 and 2024-12.
MARCH_TO_DECEMBER = Decimal("1.01") ** 9


@pytest.fixture
def seeded(database, legacy_database, monkeypatch):
    """One nominal revenue fact and one price for TEST, with prices on the test database."""
    conn, company, run = database
    monkeypatch.setattr(prices, "connect_metadata_db", legacy_database)
    filing = conn.execute(
        """INSERT INTO bronze.statement_filings
           (company_id,fiscal_year,fiscal_quarter,statement_type,source_filename,source_path,source_sha256)
           VALUES (%s,2024,1,'income_statement','test.xls','test.zip/test.xls','test')
           RETURNING filing_key""",
        (company,),
    ).fetchone()["filing_key"]
    store_context(conn, filing, run, {
        "measurement_basis": "nominal", "purchasing_power_date": None,
        "evidence": {"source": "synthetic nominal fixture"},
    })
    conn.execute(
        """INSERT INTO silver.financial_observations
           (company_id,filing_key,processing_run_id,statement_type,fiscal_year,fiscal_quarter,
            period_start,period_end,line_item_path,item,amount)
           VALUES (%s,%s,%s,'income_statement',2024,1,'2024-01-01','2024-03-31',
                   'profit or loss > hasılat','hasılat',100)""",
        (company, filing, run),
    )
    conn.execute(
        """INSERT INTO ops.active_filing_versions
           (company_id,statement_type,fiscal_year,fiscal_quarter,filing_key,processing_run_id)
           VALUES (%s,'income_statement',2024,1,%s,%s)""",
        (company, filing, run),
    )
    conn.execute(
        """UPDATE silver.monthly_macro_data SET value=1
           WHERE series_code='CPI_MONTHLY_RATE' AND year=2024"""
    )
    pipeline._ensure_instruments(conn)
    instrument = conn.execute(
        "SELECT instrument_id FROM metadata.instruments WHERE symbol='TEST'"
    ).fetchone()["instrument_id"]
    conn.execute(
        """INSERT INTO silver.daily_prices (instrument_id,price_date,close,source)
           VALUES (%s,'2024-03-29',10,'IsYatirim')""",
        (instrument,),
    )
    conn.commit()
    return conn


def published(conn):
    """Target months and real values currently published, after committing reads."""
    conn.commit()
    values = conn.execute(
        """SELECT inflation_base_year y,inflation_base_month m,value_real
           FROM gold.current_financial_values WHERE item='hasılat'"""
    ).fetchall()
    series = conn.execute(
        """SELECT inflation_base_year y,inflation_base_month m,close_real
           FROM gold.current_price_series"""
    ).fetchall()
    return values, series


def stored(conn):
    conn.commit()
    return levels.stored_price_level(conn)


def test_first_run_on_an_empty_database_adopts_the_latest_cpi_month(seeded):
    assert stored(seeded) is None
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    assert stored(seeded) == (2026, 12)
    values, _ = published(seeded)
    assert {(r["y"], r["m"]) for r in values} == {(2026, 12)}
    history = seeded.execute("SELECT * FROM ops.price_level_history").fetchall()
    assert [(h["target_year"], h["target_month"], h["details"]["source"]) for h in history] == [
        (2026, 12, "latest_cpi_month")
    ]


def test_runs_use_the_stored_month_and_warn_when_newer_cpi_exists(seeded, price_level, caplog):
    price_level(2024, 3)
    with caplog.at_level(logging.WARNING):
        pipeline.calculate_gold_financials_postgres(companies=TEST)
        prices.adjust_prices_postgres()
    values, series = published(seeded)
    assert {(r["y"], r["m"]) for r in values + series} == {(2024, 3)}
    assert values[0]["value_real"] == 100 and series[0]["close_real"] == 10
    assert "set-price-level 2026-12" in caplog.text
    assert stored(seeded) == (2024, 3)


def test_rebase_recalculates_financials_and_prices_then_switches(seeded, price_level):
    price_level(2024, 3)
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    prices.adjust_prices_postgres()

    result = levels.set_price_level(2024, 12)

    assert result["previous"] == (2024, 3) and result["companies"] == 1
    assert stored(seeded) == (2024, 12)
    values, series = published(seeded)
    assert {(r["y"], r["m"]) for r in values + series} == {(2024, 12)}
    assert values[0]["value_real"] == pytest.approx(100 * MARCH_TO_DECEMBER, rel=Decimal("1e-6"))
    assert series[0]["close_real"] == pytest.approx(10 * MARCH_TO_DECEMBER, rel=Decimal("1e-6"))
    runs = {
        r["pipeline_name"]: r["status"]
        for r in seeded.execute(
            "SELECT pipeline_name,status FROM ops.pipeline_runs WHERE run_id IN (%s,%s,%s)",
            (result["run_id"], result["gold_run_id"], result["price_run_id"]),
        )
    }
    assert runs == {
        "price_level_rebase": "succeeded",
        "gold_financial_calculation": "succeeded",
        "gold_price_adjustment": "succeeded",
    }
    latest = seeded.execute(
        "SELECT * FROM ops.price_level_history ORDER BY history_id DESC LIMIT 1"
    ).fetchone()
    assert (latest["previous_year"], latest["previous_month"]) == (2024, 3)
    assert latest["details"]["gold_run_id"] == result["gold_run_id"]


def test_failed_rebase_leaves_the_previous_month_and_data_published(seeded, price_level, monkeypatch):
    price_level(2024, 3)
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    prices.adjust_prices_postgres()
    before = published(seeded)

    def fail(*args, **kwargs):
        raise RuntimeError("price stage failed")

    monkeypatch.setattr(prices, "_adjust_price_stages", fail)
    with pytest.raises(RuntimeError, match="price stage failed"):
        levels.set_price_level(2024, 12)

    assert stored(seeded) == (2024, 3)
    assert published(seeded) == before
    status = seeded.execute(
        """SELECT status FROM ops.pipeline_runs WHERE pipeline_name='price_level_rebase'
           ORDER BY run_id DESC LIMIT 1"""
    ).fetchone()["status"]
    assert status == "failed"


def test_rebase_rejects_a_month_without_cpi(seeded, price_level):
    price_level(2024, 3)
    with pytest.raises(levels.PriceLevelError, match="No CPI data for target month 2030-01"):
        levels.set_price_level(2030, 1)
    assert stored(seeded) == (2024, 3)


def test_mixed_published_months_require_an_explicit_rebase(seeded, price_level):
    price_level(2024, 3)
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    prices.adjust_prices_postgres()
    # A database from before the project price level, published at two months.
    seeded.execute("DELETE FROM ops.price_level")
    seeded.execute("UPDATE gold.price_series SET inflation_base_month=6")
    seeded.commit()

    with pytest.raises(levels.PriceLevelError, match="several target months"):
        pipeline.calculate_gold_financials_postgres(companies=TEST)
    assert stored(seeded) is None

    levels.set_price_level(2024, 6)
    values, series = published(seeded)
    assert {(r["y"], r["m"]) for r in values + series} == {(2024, 6)}


def test_existing_single_month_database_adopts_its_published_month(seeded, price_level):
    price_level(2024, 3)
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    seeded.execute("DELETE FROM ops.price_level")
    seeded.commit()
    prices.adjust_prices_postgres()
    assert stored(seeded) == (2024, 3)
    _, series = published(seeded)
    assert {(r["y"], r["m"]) for r in series} == {(2024, 3)}


def test_a_stray_month_blocks_publication_until_rebased(seeded, price_level):
    price_level(2024, 3)
    pipeline.calculate_gold_financials_postgres(companies=TEST)
    prices.adjust_prices_postgres()
    seeded.execute("UPDATE gold.price_series SET inflation_base_month=4")
    seeded.commit()
    active = seeded.execute(
        "SELECT run_id FROM ops.active_company_versions WHERE dataset_name='financials'"
    ).fetchone()["run_id"]

    with pytest.raises(levels.PriceLevelError, match="not at the project price level 2024-03"):
        pipeline.calculate_gold_financials_postgres(companies=TEST)

    seeded.commit()
    assert seeded.execute(
        "SELECT run_id FROM ops.active_company_versions WHERE dataset_name='financials'"
    ).fetchone()["run_id"] == active
    status = levels.price_level_status()
    assert status["stored"] == (2024, 3)
    assert status["mismatches"]["price_series"] == 1


@pytest.mark.parametrize(
    "target, expected_real",
    [
        # Target is the latest CPI month: newer prices are used as they are.
        ((2026, 12), Decimal(20)),
        # Older target: the carried level is deflated like a December 2026 price.
        ((2024, 3), Decimal(20) / MARCH_TO_DECEMBER),
    ],
)
def test_prices_after_the_latest_cpi_month_carry_its_level_forward(
    seeded, price_level, target, expected_real
):
    instrument = seeded.execute(
        "SELECT instrument_id FROM metadata.instruments WHERE symbol='TEST'"
    ).fetchone()["instrument_id"]
    seeded.execute(
        """INSERT INTO silver.daily_prices (instrument_id,price_date,close,source)
           VALUES (%s,'2027-01-05',20,'IsYatirim')""",
        (instrument,),
    )
    seeded.commit()
    price_level(*target)
    run = prices.adjust_prices_postgres()

    rows = {
        str(r["price_date"]): r
        for r in seeded.execute(
            "SELECT price_date,close_real,close_real_method FROM gold.current_price_series"
        )
    }
    assert rows["2027-01-05"]["close_real_method"] == "latest_cpi_carried_forward"
    assert rows["2027-01-05"]["close_real"] == pytest.approx(expected_real, rel=Decimal("1e-6"))
    assert rows["2024-03-29"]["close_real_method"] == "cpi"
    assert all(r["close_real"] is not None for r in rows.values())
    check = seeded.execute(
        """SELECT affected_rows,details FROM ops.data_quality_results
           WHERE run_id=%s AND check_name='price_rows_after_latest_cpi'""",
        (run,),
    ).fetchone()
    assert check["affected_rows"] == 1 and check["details"]["first_price_date"] == "2027-01-05"


def test_a_waiting_run_reads_the_month_only_after_the_lock(seeded, price_level, legacy_database):
    price_level(2024, 3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with legacy_database() as rebase:  # stands in for a rebase holding the lock
            levels.lock_price_level(rebase)
            run = executor.submit(pipeline.calculate_gold_financials_postgres, companies=TEST)
            time.sleep(1)
            assert not run.done(), "the Gold run must wait for the price-level lock"
            rebase.execute("UPDATE ops.price_level SET target_month=12")
        run.result(timeout=60)  # re-raises any error from the Gold run
    values, _ = published(seeded)
    assert {(r["y"], r["m"]) for r in values} == {(2024, 12)}


@pytest.mark.parametrize(
    "text, expected",
    [("2026-10", (2026, 10)), (" 2024-3 ", (2024, 3))],
)
def test_parse_price_level(text, expected):
    assert levels.parse_price_level(text) == expected


@pytest.mark.parametrize("text", ["2026", "2026-13", "2026-00", "oct-2026", "2026-10-01"])
def test_parse_price_level_rejects_invalid_months(text):
    with pytest.raises(ValueError):
        levels.parse_price_level(text)


@pytest.mark.parametrize(
    "arguments",
    [
        ["calculate-gold", "--price-level-year", "2026", "--price-level-month", "10"],
        ["update-prices", "--begin-date", "2026-01-01", "--end-date", "2026-02-01",
         "--price-level-year", "2026", "--price-level-month", "10"],
        ["update-database", "--begin-year", "2026", "--begin-quarter", "1", "--end-year", "2026",
         "--end-quarter", "2", "--price-level-year", "2026", "--price-level-month", "10"],
        ["adjust-prices", "--price-level-year", "2026", "--price-level-month", "10"],
        ["set-price-level", "2026-13"],
    ],
)
def test_cli_no_longer_accepts_a_target_month_elsewhere(arguments):
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(arguments)
    assert error.value.code == 2


def test_cli_set_price_level_and_update_prices_use_the_project_level(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "set_price_level", lambda year, month: calls.append(("set", year, month)) or {
        "price_level": (year, month), "previous": (2026, 9), "companies": 3, "run_id": 7,
    })
    cli.main(["set-price-level", "2026-10"])
    assert "2026-10 (was 2026-09)" in capsys.readouterr().out

    monkeypatch.setattr(cli, "resolve_companies", lambda symbols: [SimpleNamespace(symbol="TEST")])
    monkeypatch.setattr(cli, "resolve_price_symbols", lambda symbols: ["TEST"])
    monkeypatch.setattr(cli, "update_prices_postgres", lambda *a, **k: calls.append(("download", a[0])))
    monkeypatch.setattr(cli, "adjust_prices_postgres", lambda symbols: calls.append(("adjust", symbols)))
    cli.main(["update-prices", "--symbols", "TEST", "--begin-date", "2026-01-01", "--end-date", "2026-02-01"])
    assert calls == [("set", 2026, 10), ("download", ["TEST"]), ("adjust", ["TEST"])]
