# AGENTS.md

## Project map

This Python project publishes BIST company data from KAP to one PostgreSQL database, `bist_finance`. It has two pipelines that share issuer metadata.

- [bist/companies/](bist/companies/) owns the shared `metadata` schema: the `Company` model, the database connection (`metadata_store.connect_metadata_db`), KAP metadata refresh (`update_company_info`), alias resolution (`loader`), and the JSON seed import. Both pipelines depend on it, so keep changes backward compatible for both.
- [bist/financials/](bist/financials/) is the financial statement ETL. [cli.py](bist/financials/cli.py) defines the CLI (`python -m bist.financials <command> --help`). `run` refreshes reference data, downloads KAP archives, and publishes financial data. `update-prices` fetches prices separately. [parsing/](bist/financials/parsing/) locates annual archives, parses KAP tables and EPS disclosures, and selects canonical facts. [models/](bist/financials/models/) holds calculation logic. [services/](bist/financials/services/) handles ingestion, PostgreSQL publication, and recalculation.
- [bist/activity_reports/](bist/activity_reports/) is the activity report pipeline. [fetcher.py](bist/activity_reports/fetcher.py) indexes disclosures and downloads PDFs; [processor.py](bist/activity_reports/processor.py) ingests text, extracts facts with OpenAI, and embeds chunks. Both are CLIs (`python -m bist.activity_reports.<module> --help`).
- [bist/http_utils.py](bist/http_utils.py) holds shared HTTP retry and rate-limit helpers. [bist/paths.py](bist/paths.py) defines every filesystem location.
- [database/migrations/](database/migrations/) holds all SQL. Numbered files are the metadata bootstrap and Bronze/Silver/Gold/Ops migrations; register new medallion migrations in `MIGRATIONS` in [postgres_financial_pipeline.py](bist/financials/services/postgres_financial_pipeline.py). `activity_reports_NNN.sql` files are applied automatically in name order by [store.py](bist/activity_reports/store.py).
- [data_mappings/](data_mappings/) holds statement mappings and the company metadata seed.
- [README.md](README.md) covers setup, layout, and testing. [docs/financial_statements.md](docs/financial_statements.md) documents the financial pipeline in Turkish and English; update both sections when a change affects documented behavior. [docs/activity_reports.md](docs/activity_reports.md) documents the activity report pipeline.

## Development and verification

Use Python 3.11+ and PostgreSQL 15+. Run commands from the repository root with the project virtual environment active.

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

- Financial PostgreSQL tests use [tests/financials/conftest.py](tests/financials/conftest.py). Set `BIST_TEST_POSTGRES_DSN` to a test-server connection with `CREATEDB` privileges; the fixtures create and drop disposable databases.
- Activity report PostgreSQL tests use [tests/activity_reports/conftest.py](tests/activity_reports/conftest.py). Set `BIST_TEST_DSN` to a separate, disposable `bist_finance` database that already has the shared schema.
- PostgreSQL tests are skipped when their variable is unset. No test calls paid APIs.
- The CLIs connect through `BIST_FINANCE_DSN`, then `DATABASE_URL`, then the local `dbname=bist_finance` default. For CLI smoke tests that write data, explicitly set `BIST_FINANCE_DSN` to a disposable `bist_finance` database. `python -m bist.financials init-postgres` applies the shared schema and seeds empty metadata; the optional `--create-database` flag requires `CREATEDB`.
- Keep downloads and backups out of Git: archives under `financial_statements/<SYMBOL>/<SYMBOL>_<YEAR>.zip`, PDFs under `activity_reports/<SYMBOL>/`, and pre-repair table exports under `database_backups/`. The local checkout holds the full downloaded set (about 33 GB), so use retained files for offline checks. `run --skip-download --skip-reference-data` reuses retained archives and reference data.

## Financial data and publication

Read the [financial-data instructions](.github/instructions/financial-data.instructions.md) before changing parsers, calculations, or database publication. They document source-specific cases and repair safeguards.

- Preserve Bronze source evidence, statement hierarchy, and reported quarter and YTD observations independently. Derive flows only from compatible fiscal dates, currencies, consolidation scopes, and measurement bases. Use actual fiscal period-end dates for CPI and FX, require complete CPI spans and appropriate period continuity, and leave unsupported results null.
- Use disclosed EPS units and period-specific weighted-average denominators. Derive quarters from earnings and share-day inputs when supported. Statement monetary multipliers and current company share metadata are unsuitable for historical EPS.
- Publish verified total basic ordinary-share EPS first and use the corresponding historical-capital estimate as a fallback for each quarter, YTD, and TTM field in `eps`. Keep detailed verified observations and estimate evidence in `gold.current_eps_values` and `gold.current_eps_coverage`, respectively. Rebase `eps_inflation_adj` from the selected field's own purchasing-power basis.
- Historical P/E uses the original İş Yatırım close (`HG_KAPANIS`) on or within ten calendar days before the actual fiscal period end. Keep verified P/E in the financial ratios and estimated P/E with its quality in `gold.current_price_earnings`. The valuation is retrospective.
- Every inflation-adjusted value uses one project-wide CPI target month stored in `ops.price_level` ([price_level.py](bist/financials/services/price_level.py)). Never add a way to pass a target month to Gold or price runs: read it with `require_price_level` while holding `lock_price_level`, run `check_price_level` before publishing, and change it only through `set-price-level`, which recalculates everything in one transaction.
- Use Gold `current_*` views to inspect active outputs. Preserve Bronze/Silver history and publication transaction boundaries. Before a production repair, back up active publication pointers and the derived data in scope. Limit repair republication to companies with active Gold data unless the user requests broader publication.
