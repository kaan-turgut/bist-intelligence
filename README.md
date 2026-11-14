# BIST Intelligence

Data pipelines for companies listed on Borsa İstanbul (BIST). Both pipelines read from KAP
(Public Disclosure Platform) and write to one shared PostgreSQL database, `bist_finance`.

| Pipeline | What it does | Guide |
| --- | --- | --- |
| **Financial statements** | Downloads KAP financial statement archives, parses statements and EPS disclosures, adds CPI/USD reference data and İş Yatırım prices, and publishes Bronze/Silver/Gold financial values, ratios, EPS and P/E. | [docs/financial_statements.md](docs/financial_statements.md) (Türkçe / English) |
| **Activity reports** | Indexes and downloads KAP activity report PDFs, extracts and chunks their text, and uses an LLM to extract facts about operations, strategy, investment, risks, governance and outlook. | [docs/activity_reports.md](docs/activity_reports.md) |

Both pipelines share issuer identity (MKK OID, ticker aliases, sectors, indexes) from the
`metadata` schema, which is maintained by `bist.companies`.

**This project does not predict share prices or give investment advice.** Check important
results against the original KAP filings.

## Setup

You need Python 3.11+ and PostgreSQL 15+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt   # runtime only: requirements.txt
export BIST_FINANCE_DSN="dbname=bist_finance host=localhost user=YOUR_USER"
export OPENAI_API_KEY="YOUR_API_KEY"            # activity report extraction only
```

On Windows, create the environment with `py -3.11 -m venv .venv`, activate it with
`.\.venv\Scripts\Activate.ps1`, and set variables with `$env:BIST_FINANCE_DSN = "..."`.
[.env.example](.env.example) lists every variable; `.env` files are not loaded automatically.

The connection uses `BIST_FINANCE_DSN`, then `DATABASE_URL`, then the local default
`dbname=bist_finance`. Any other database name is rejected.

Run every command from the repository root.

### Create the database

```bash
python -m bist.financials init-postgres --create-database   # metadata, Bronze/Silver/Gold/Ops, seed companies
python -m bist.activity_reports.processor migrate-db         # activity report tables
```

All SQL migrations live in [database/migrations/](database/migrations/). Numbered files
(`001_…`–`017_…`) belong to the metadata and financial schemas; `activity_reports_*.sql`
files extend the same database and are tracked in `ops.schema_migrations`.

## Common commands

```bash
# Shared company metadata
python -m bist.companies.update_company_info --symbols THYAO

# Financial statements: download, parse and publish
python -m bist.financials run --symbols THYAO --begin-year 2017 --begin-quarter 1 --end-year 2026 --end-quarter 2
python -m bist.financials update-prices --symbols THYAO --begin-date 2017-01-01 --end-date 2026-06-30
python -m bist.financials calculate-pe --symbols THYAO

# Activity reports: index, download, ingest and extract
python -m bist.activity_reports.fetcher index-and-download --symbols THYAO --begin-date 2017-01-01 --end-date "$(date +%F)"
python -m bist.activity_reports.processor ingest --symbols THYAO
python -m bist.activity_reports.processor extract --symbols THYAO
```

Add `--help` to any command for its options. The guides in [docs/](docs/) describe every
command, the published tables and views, and the data-quality safeguards.

## Project layout

```text
bist/
  paths.py, logging_config.py, http_utils.py   Shared paths, logging and HTTP retry helpers
  companies/          Shared issuer metadata: Company model, bist_finance connection,
                      KAP metadata refresh, alias resolution, JSON seed import
  financials/         Financial statement pipeline (CLI: python -m bist.financials)
    cli.py            Command-line entry point
    models/           EPS, capital, P/E and financial value calculations
    parsing/          Locating KAP archives, parsing statements and EPS disclosures
    services/         Downloads, PostgreSQL publication, prices, repairs
  activity_reports/   Activity report pipeline
    fetcher.py        KAP disclosure index and PDF downloads (CLI)
    processor.py      Text ingestion, LLM extraction, embeddings (CLI)
    catalog.py, store.py, fact_analysis.py
database/migrations/  SQL migrations for the shared bist_finance database
data_mappings/        KAP statement mappings and seed company metadata
docs/                 Pipeline guides and the activity report database guide (PDF)
notebooks/            Financial statement validation notebook
tests/
  financials/         Financial pipeline tests
  activity_reports/   Activity report pipeline tests
financial_statements/ Downloaded KAP archives: <SYMBOL>/<SYMBOL>_<YEAR>.zip (not in Git)
activity_reports/     Downloaded activity report PDFs: <SYMBOL>/<SYMBOL>_<YEAR>_<PERIOD>_<INDEX>.pdf (not in Git)
```

## Development

```bash
python -m pytest
ruff check .
```

PostgreSQL integration tests are skipped unless their test database is configured. No test
calls paid APIs, and neither suite touches the database in `BIST_FINANCE_DSN`.

- **Financial tests** (`tests/financials/`): set `BIST_TEST_POSTGRES_DSN` to a server
  connection whose role has `CREATEDB`. The fixtures create and drop a throwaway database
  for each test module.
- **Activity report tests** (`tests/activity_reports/`): set `BIST_TEST_DSN` to a
  **separate, disposable** database named `bist_finance` that already has the shared schema.
  The tests delete activity report rows there and refuse to run against the configured
  application database.

## License

[MIT](LICENSE)
