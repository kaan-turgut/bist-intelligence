"""One project-wide CPI target month ("price level") for every inflation-adjusted value.

Every published inflation-adjusted quantity (Gold financial values and ratios,
``eps_inflation_adj`` and real prices) must share one target month. The month is
stored once in ``ops.price_level``. Gold and price runs read it inside their own
transaction while holding ``PRICE_LEVEL_LOCK``, so they can never publish another
month. Only :func:`set_price_level` changes it, and it recalculates every
published company and price series in the same transaction before switching.
"""

import logging
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from bist.logging_config import StepTimer

logger = logging.getLogger(__name__)

# Shared by Gold calculations, EPS refreshes, price adjustments and rebases.
PRICE_LEVEL_LOCK = "bist_finance_price_earnings"


class PriceLevelError(RuntimeError):
    """The stored price level is missing, unusable or contradicted by published data."""


def lock_price_level(conn):
    """Serialize every writer of target-dependent data until the transaction ends."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PRICE_LEVEL_LOCK,))


def format_price_level(level):
    return f"{level[0]}-{level[1]:02d}" if level else "not set"


def parse_price_level(text):
    """Parse ``YYYY-MM`` into ``(year, month)``."""
    try:
        year_text, month_text = text.strip().split("-")
        year, month = int(year_text), int(month_text)
    except (AttributeError, ValueError):
        raise ValueError(f"Expected a target month as YYYY-MM, got {text!r}.") from None
    if not 1 <= month <= 12:
        raise ValueError(f"Target month must be between 1 and 12, got {text!r}.")
    return year, month


def latest_cpi_month(conn):
    row = conn.execute(
        """SELECT year,month FROM silver.monthly_macro_data
           WHERE series_code='CPI_MONTHLY_RATE' ORDER BY observation_date DESC LIMIT 1"""
    ).fetchone()
    return (row["year"], row["month"]) if row else None


def cpi_month_exists(conn, year, month):
    return (
        conn.execute(
            """SELECT 1 FROM silver.monthly_macro_data
               WHERE series_code='CPI_MONTHLY_RATE' AND year=%s AND month=%s""",
            (year, month),
        ).fetchone()
        is not None
    )


def stored_price_level(conn):
    row = conn.execute("SELECT target_year,target_month FROM ops.price_level").fetchone()
    return (row["target_year"], row["target_month"]) if row else None


def published_price_levels(conn):
    """Distinct target months in published financial values and price series."""
    rows = conn.execute(
        """SELECT DISTINCT inflation_base_year AS year,inflation_base_month AS month
           FROM gold.current_financial_values
           UNION
           SELECT DISTINCT inflation_base_year,inflation_base_month
           FROM gold.current_price_series"""
    ).fetchall()
    return {(r["year"], r["month"]) for r in rows}


def _store_price_level(conn, level, run_id, details):
    previous = stored_price_level(conn)
    conn.execute(
        """INSERT INTO ops.price_level (singleton,target_year,target_month,run_id,set_at)
           VALUES (true,%s,%s,%s,now())
           ON CONFLICT (singleton) DO UPDATE SET target_year=EXCLUDED.target_year,
             target_month=EXCLUDED.target_month,run_id=EXCLUDED.run_id,set_at=now()""",
        (*level, run_id),
    )
    conn.execute(
        """INSERT INTO ops.price_level_history
           (previous_year,previous_month,target_year,target_month,run_id,details)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (*(previous or (None, None)), *level, run_id, Jsonb(details)),
    )


def require_price_level(conn, run_id=None):
    """Return the stored target month, adopting one only when that is unambiguous.

    Call while holding :func:`lock_price_level`. When nothing is stored yet, an
    existing database whose published data already uses a single month adopts
    it; an empty database adopts the latest CPI month. Mixed published months
    must be resolved with ``set-price-level`` because adopting either month
    would silently keep the other one published.
    """
    stored = stored_price_level(conn)
    if stored:
        return stored
    published = published_price_levels(conn)
    if len(published) > 1:
        months = ", ".join(sorted(format_price_level(level) for level in published))
        raise PriceLevelError(
            f"Published data uses several target months ({months}) and no project price "
            "level is set. Run `python -m bist.financials set-price-level YYYY-MM` to "
            "recalculate everything at one month."
        )
    if published:
        level, source = published.pop(), "published_data"
    else:
        level, source = latest_cpi_month(conn), "latest_cpi_month"
        if level is None:
            raise PriceLevelError(
                "No CPI data found in PostgreSQL. Run "
                "`python -m bist.financials update-reference-data` first."
            )
    _store_price_level(conn, level, run_id, {"source": source})
    logger.info("Project price level initialized to %s from %s", format_price_level(level), source)
    return level


def warn_if_stale(conn, level):
    """Log when newer CPI exists than the project price level."""
    latest = latest_cpi_month(conn)
    if latest and latest > level:
        logger.warning(
            "Project price level is %s but CPI is available through %s. Run "
            "`python -m bist.financials set-price-level %s` to rebase all inflation-adjusted values.",
            format_price_level(level), format_price_level(latest), format_price_level(latest),
        )
    return latest


def price_level_mismatches(conn, level):
    """Count published rows whose target month differs from ``level``."""
    year, month = level
    return conn.execute(
        """SELECT
          (SELECT count(*) FROM gold.current_financial_values
           WHERE (inflation_base_year,inflation_base_month) IS DISTINCT FROM (%(y)s,%(m)s)) AS financial_values,
          (SELECT count(*) FROM gold.current_price_series
           WHERE (inflation_base_year,inflation_base_month) IS DISTINCT FROM (%(y)s,%(m)s)) AS price_series,
          (SELECT count(*) FROM gold.eps_coverage c
           JOIN ops.active_company_versions a ON a.dataset_name='financials'
            AND a.company_id=c.company_id AND a.run_id=c.calculation_run_id
           WHERE c.inflation_adjustment->>'inflation_base_year' IS NOT NULL
             AND ((c.inflation_adjustment->>'inflation_base_year')::integer,
                  (c.inflation_adjustment->>'inflation_base_month')::integer)
                 IS DISTINCT FROM (%(y)s,%(m)s)) AS eps_inflation_adj""",
        {"y": year, "m": month},
    ).fetchone()


def check_price_level(conn, run_id, level):
    """Record a data-quality result and fail the transaction on any mismatch."""
    counts = dict(price_level_mismatches(conn, level))
    affected = sum(counts.values())
    conn.execute(
        """INSERT INTO ops.data_quality_results (run_id,check_name,severity,affected_rows,details)
           VALUES (%s,'published_price_level_mismatch',%s,%s,%s)""",
        (
            run_id,
            "error" if affected else "info",
            affected,
            Jsonb({"price_level": format_price_level(level), **counts}),
        ),
    )
    if affected:
        raise PriceLevelError(
            f"{affected} published rows are not at the project price level "
            f"{format_price_level(level)}: {counts}. Nothing from this run was published."
        )


def price_level_status():
    """Read-only summary for ``show-price-level``."""
    from bist.financials.services import postgres_financial_pipeline as financials

    financials.apply_medallion_migration()
    with financials.connect_metadata_db() as conn:
        stored = stored_price_level(conn)
        published = published_price_levels(conn)
        status = {
            "stored": stored,
            "latest_cpi": latest_cpi_month(conn),
            "published": sorted(published),
            "mismatches": dict(price_level_mismatches(conn, stored)) if stored else None,
            "history": conn.execute(
                """SELECT previous_year,previous_month,target_year,target_month,run_id,
                          details,changed_at
                   FROM ops.price_level_history ORDER BY history_id DESC LIMIT 5"""
            ).fetchall(),
        }
    return status


def published_company_symbols(conn):
    return [
        r["symbol"]
        for r in conn.execute(
            """SELECT c.symbol FROM ops.active_company_versions a
               JOIN metadata.companies c USING (company_id)
               WHERE a.dataset_name='financials' ORDER BY c.symbol"""
        )
    ]


def set_price_level(year, month):
    """Recalculate every inflation-adjusted value at a new month, then switch to it.

    Everything happens in one transaction: published companies get a new Gold
    run (values, ratios, EPS, ``eps_inflation_adj`` and P/E), every price series
    is re-adjusted, and only then is the stored month replaced. A failure rolls
    all of it back and the previous month stays in force.
    """
    # Connect through the pipeline module, the one connection factory every
    # writer shares (tests redirect it to a disposable database).
    from bist.financials.services import postgres_financial_pipeline as financials
    from bist.financials.services import postgres_price_pipeline as prices

    year, month = int(year), int(month)
    if not 1 <= month <= 12:
        raise ValueError("Target month must be between 1 and 12.")
    level = (year, month)
    financials.apply_medallion_migration()
    with financials.connect_metadata_db() as conn:
        rebase_run = financials._run_start(
            conn, "price_level_rebase", {"price_level": [year, month]}
        )
        conn.commit()
        try:
            lock_price_level(conn)
            if not cpi_month_exists(conn, year, month):
                raise PriceLevelError(f"No CPI data for target month {format_price_level(level)}.")
            previous = stored_price_level(conn)
            companies = [SimpleNamespace(symbol=s) for s in published_company_symbols(conn)]
            logger.info(
                "Rebasing from %s to %s: %s published companies and all price series",
                format_price_level(previous), format_price_level(level), len(companies),
            )
            timer = StepTimer(logger, "Price level rebase")
            gold_run = None
            if companies:
                gold_run = financials._run_start(conn, "gold_financial_calculation", {
                    "price_level": [year, month],
                    "symbols": [c.symbol for c in companies],
                    "price_level_rebase_run_id": rebase_run,
                })
                financials._calculate_gold_stages(conn, gold_run, year, month, companies)
                financials._run_finish(conn, gold_run)
                timer.mark(f"Gold recalculation for {len(companies)} companies")
            price_run = None
            if conn.execute("SELECT 1 FROM silver.daily_prices LIMIT 1").fetchone():
                price_run = prices._run_start(conn, "gold_price_adjustment", {
                    "price_level": [year, month],
                    "symbols": None,
                    "price_level_rebase_run_id": rebase_run,
                })
                prices._adjust_price_stages(conn, price_run, year, month, None)
                prices._run_finish(conn, price_run)
                timer.mark("price series recalculation")
            _store_price_level(conn, level, rebase_run, {
                "source": "set_price_level",
                "gold_run_id": gold_run,
                "price_run_id": price_run,
                "companies": len(companies),
            })
            check_price_level(conn, rebase_run, level)
            timer.mark("consistency check")
            financials._run_finish(conn, rebase_run)
            conn.commit()
            timer.mark("commit")
        except BaseException as exc:
            conn.rollback()
            financials._run_finish(conn, rebase_run, exc)
            conn.commit()
            raise
    logger.info(
        "Project price level is now %s (was %s); rebase run %s",
        format_price_level(level), format_price_level(previous), rebase_run,
    )
    return {
        "run_id": rebase_run,
        "previous": previous,
        "price_level": level,
        "gold_run_id": gold_run,
        "price_run_id": price_run,
        "companies": len(companies),
    }
