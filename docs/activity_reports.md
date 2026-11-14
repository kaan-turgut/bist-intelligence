# BIST Activity Report Intelligence Pipeline

Collect KAP activity-report PDFs and extract analyst-relevant facts about operations,
strategy, investment, risks, governance, and outlook. The project writes to the existing
**`bist_finance` PostgreSQL database**, shared with the [financial statement pipeline](financial_statements.md).
Company identity, aliases, sectors, and indexes come from its `metadata` schema.

PDFs stay on disk. The disclosure catalog, search progress, download state, company
information, extracted text, facts, and embeddings all use PostgreSQL. There is no
runtime JSON cache or SQLite dependency.

## Setup

Python 3.11+ and the shared PostgreSQL metadata/ops schemas are required. Install the project and set `BIST_FINANCE_DSN` as described in the [root README](../README.md#setup), and also export `OPENAI_API_KEY` for extraction and embeddings ([.env.example](../.env.example) lists both).

The connector uses `BIST_FINANCE_DSN`, then `DATABASE_URL`, then libpq's default
`dbname=bist_finance` settings. It rejects any other database name. Standard libpq
password files also work. `.env` files are not loaded automatically. Keep credentials out of source control.

A printable overview of the report tables is in [activity_report_database_guide.pdf](activity_report_database_guide.pdf).

## Initialize the report schema

```bash
python -m bist.activity_reports.processor migrate-db
```

Versioned SQL migrations extend the shared database and are tracked in
`ops.schema_migrations`. Normal processor commands also apply pending report migrations.
The shared database must already contain the metadata and ops schemas created by `python -m bist.financials init-postgres`.

API credentials come only from `OPENAI_API_KEY` in the environment.

## Workflow

Company metadata is already shared. Refresh it when needed; this command writes to the
same tables and follows the same MKK identity conventions as the financial statement pipeline:

```bash
python -m bist.companies.update_company_info --symbols THYAO
```

Index disclosures and download their PDFs:

```bash
python -m bist.activity_reports.fetcher index-and-download --symbols THYAO FROTO --begin-date 2017-01-01 --end-date "$(date +%F)" --begin-year 2017 --begin-period 1 --end-year 2026 --end-period 2 --request-sleep 3 --rate-limit-backoff 300
```

Omit `--symbols` to use every company in PostgreSQL. The date range filters KAP
**publication dates**; the year/period bounds filter the **reporting period** of PDFs
downloaded. Searching through today can discover Q2 reports published after June 30.

On macOS, prevent idle sleep while filling missing reports through 2026 Q2:

```bash
caffeinate -i .venv/bin/python -m bist.activity_reports.fetcher index-and-download \
  --begin-date 2017-01-01 --end-date "$(date +%F)" \
  --begin-year 2017 --begin-period 1 --end-year 2026 --end-period 2 \
  --request-sleep 3 --rate-limit-backoff 300
```

### Search caching and safe resumption

To index missing search ranges without downloading PDFs or running AI extraction:

```bash
caffeinate -i .venv/bin/python -m bist.activity_reports.fetcher index \
  --begin-date 2017-01-01 --end-date "$(date +%F)"
```

The default HTTP 429 base wait is 300 seconds. Consecutive retries increase that wait
(300, 600, 900, 1200 seconds); a numeric KAP `Retry-After` header takes precedence.
Use `--rate-limit-backoff` to override the base wait.

Successful KAP searches and their returned disclosures are committed together, with
coverage recorded by **company, query version, and inclusive publication-date range**.
Subsequent runs subtract covered intervals and search only the gaps, even if the company
selection, request batch size, or date bounds change. Successful empty searches also count.
Failed requests, invalid responses, or rolled-back writes never establish coverage.
If the request filter contract changes, increment `QUERY_VERSION` in the catalog module.

The current Istanbul calendar day is never marked complete. It is searched again on the
next run so later disclosures can be found; future dates are not checkpointed. To refresh
older completed ranges for retrospective KAP changes, add `--refresh-index`:

```bash
python -m bist.activity_reports.fetcher index --symbols THYAO \
  --begin-date 2017-01-01 --end-date "$(date +%F)" --refresh-index
```

Historical disclosures whose source codes map to no current issuer are retained with
`company_id` NULL. They are not guessed to belong to a current issuer; normal
company-based downloads exclude them until a valid issuer mapping is available.

HTTP 429 and 5xx responses and transient network errors are retried with backoff.
Other client errors (404, 401, ...) are never retried, because repeating the request
cannot change the answer.

### Download behavior

`download` reads only the saved PostgreSQL catalog and does not search KAP:

```bash
python -m bist.activity_reports.fetcher download \
  --begin-year 2017 --begin-period 1 --end-year 2026 --end-period 2 \
  --request-sleep 3 --rate-limit-backoff 300
```

Both this command and `index-and-download` perform no AI extraction. Missing attachment
links are retried without redoing completed disclosure searches. Known links are reused.
Download state is separate from PDF text ingestion, so downloading does not create chunks
or facts. Existing PDFs are checked on disk and registered/skipped; deleted files are
downloaded again even if their old database state says `downloaded`.

Files are written to a temporary `.part` file, checked for PDF header/EOF markers, then
renamed into place. This is a basic transport-integrity check, not full PDF validation.
Invalid bodies, interrupted jobs, and failed downloads remain retryable. HTTP 404/410
failures clear the saved URL so the next run can resolve a fresh link. The attempt counter
counts download-stage attempts, while catalog-only link lookups are not counted as PDF
download attempts. `downloaded_at` records the first successful local registration.
An explicit `--output-dir` is respected; paths outside that directory are not used to
skip a requested destination. Partial failures are logged and produce a failed pipeline
run/nonzero command exit while preserving earlier completed work.

Ingest downloaded PDFs into PostgreSQL:

```bash
python -m bist.activity_reports.processor ingest --symbols THYAO FROTO
```

By default `ingest` processes only new PDFs (`--only-new`): a PDF is skipped when
PostgreSQL already holds a document with the same company, year, quarter and file hash.
A revised PDF has a new hash, so it is picked up. Add `--no-only-new` to re-parse and
refresh every matching PDF, which you need after changing `--min-chars`, `--target-chars`,
`--max-chars` or `--overlap-chars`, because skipped PDFs keep their old chunking.
Parsing is CPU-bound, so it uses
worker processes (threads would be serialized by the GIL): `--workers N`, default CPU
count - 1 up to 8, `--workers 1` to disable. PDFs are parsed in parallel while the
main process writes to PostgreSQL, one transaction per PDF. A PDF that cannot be parsed
(for example a corrupt file) is logged and skipped; the rest are saved and the command
exits nonzero.

Extract structured facts, or create optional embeddings:

```bash
python -m bist.activity_reports.processor extract --symbols THYAO --model gpt-6-luna --extraction-version gpt6_luna_v1
python -m bist.activity_reports.processor embed --symbols THYAO
```

Preview what a run would send, with no API calls and no writes:

```bash
python -m bist.activity_reports.processor extract --dry-run --symbols THYAO FROTO
```

Every real run also logs how many chunks are already extracted and skipped versus
pending, with a rough token estimate, before the first request.

Each fact has a category and event type, 1-3 `themes` from a fixed list, named
`entities`, a `magnitude` (1-10, defined in the prompt), sentiment, time relevance, an
English summary, the source quote and page, and every stated figure as structured
`metrics` (name, value, unit, period). Q4 reports are labelled as annual reports for
the model. The default extraction version is `gpt6_luna_v2`; facts from
`gpt6_luna_v1` have free-form `tags` and a `confidence` score instead of
themes/entities/metrics, and stay queryable by filtering `extraction_version`.

Extraction and embeddings make paid API calls. They run `--workers N` requests
concurrently (default 6; `--workers 1` for one at a time). Only the HTTP call runs in
worker threads; all database writes happen on the main thread over a single
connection. An HTTP 429 from OpenAI pauses every worker, not just one, and
`--request-sleep` is the minimum spacing between request starts across all workers
(default 0.25 s for extract, 0.05 s for embed). If you still see many rate-limit
warnings, lower `--workers`. After a fatal error, repeated failures or Ctrl+C, no new
request is started, but requests already in flight are saved (press Ctrl+C twice to
abort at once). Each completed chunk is committed
immediately, so an error never discards paid results. A failed chunk is recorded and
skipped, and the run continues with the next one. The run stops early only for errors
that would repeat for every chunk (invalid key, exhausted quota, unknown model) or after
five consecutive failures, then exits nonzero. Rerunning retries only the chunks that
are still pending. Requests retry transient 429/5xx/network errors, and truncated,
refused, or empty model responses are treated as failures. `ingest` likewise isolates
failures per PDF. The default extraction version is
`gpt6_luna_v1`, matching the migrated results. A completed chunk is skipped only when
its model, extraction version, and prompt hash match. Successful empty results are also
skipped. Use `--limit 1` to process one pending chunk or `--force` to repeat extraction.
A forced attempt that fails preserves the previously completed results.

Documents use company/period/content identity, so moving a PDF does not duplicate it.
Changed PDFs create new document versions. Changed chunking retires superseded chunks
without deleting their saved facts or embeddings. `ingest --force` refreshes chunk
membership while retaining reusable results; `extract --force` explicitly replaces facts
for the same chunk/model/version/prompt after a successful response.

Extraction/embedding process only the latest disclosure per company and quarter, with
newer document IDs breaking ties for revised content. Work commits per document or chunk,
so a later failure preserves completed work. Runs and failures are recorded in `ops`.
A database advisory lock permits one report writer at a time to prevent duplicate API
requests. Requests execute outside database transactions.
The KAP indexer/downloader has its own advisory lock, independent of the extraction writer.

### Novelty, duplicates and quality checks

These run on facts already stored, with no re-extraction:

```bash
python -m bist.activity_reports.processor analyze-facts --symbols THYAO
python -m bist.activity_reports.processor quality-check --symbols THYAO --sample 100
```

`analyze-facts` embeds each fact summary once (`text-embedding-3-small`, 512
dimensions, batched) and compares it with the same company's previous four reports:
`novelty` is `new`, `updated` (similar topic, different content such as new figures),
`repeated`, or `first_report`. Near-identical facts in the same report get
`duplicate_of`, pointing to the first one. Labels are recomputed locally on every run,
so they stay correct when reports are extracted out of order. Similarity thresholds are
heuristics (`--repeated-threshold 0.90`, `--related-threshold 0.80`,
`--duplicate-threshold 0.95`); tune them after looking at some labelled facts.

`quality-check` asks an LLM judge to score a random sample of not-yet-checked facts
against their source chunk (supported or not, score 1-5, a short issue) and prints a
summary with the worst cases. Each check sends the whole chunk, so 100 checks cost
roughly as much as 100 extraction requests. Verdicts are stored in
`ops.activity_report_quality_checks`.

Useful filters on `gold.current_activity_report_facts`:
`novelty IN ('new', 'updated')` for what changed, `duplicate_of IS NULL` to drop
within-report duplicates. `gold.current_activity_report_metrics` has one row per
stated figure for time series.

Explicitly clearing report data requires `--yes`:

```bash
python -m bist.activity_reports.processor clear --symbols THYAO --yes
```

This deletes only report records and their dependent rows, never shared company or
financial data. Omitting symbols clears all reports.

## PostgreSQL datasets

| Dataset | Purpose |
| --- | --- |
| `metadata.activity_report_disclosures` | KAP disclosure ID, issuer, report period, publication time, source payload, URL, and discovery timestamps |
| `ops.activity_report_downloads` | Per-disclosure download state, local path/hash/size, attempts, timestamps, errors, and pipeline run |
| `ops.activity_report_index_searches` | Successful closed-day search coverage by issuer, query contract, and date interval |
| `bronze.activity_report_documents` | Company, quarter, disclosure ID, source hash, PDF path, ingestion run |
| `bronze.activity_report_pages` | Retained PDF page text and original page numbers |
| `silver.activity_report_chunks` | Overlapping text chunks, native page-number arrays, active membership |
| `ops.activity_report_extraction_runs` | Model/version/prompt completion, counts, errors, pipeline run |
| `silver.activity_report_extractions` | Facts, JSONB tags, source quotes, confidence, and page validity |
| `silver.activity_report_embeddings` | Native numeric embedding arrays keyed by chunk/model |
| `silver.current_activity_report_documents` | Latest source disclosure for each company/quarter |
| `gold.current_activity_report_facts` | Completed active facts from the latest documents and active chunks |

The gold view exposes all active model/version combinations; filter those columns when
comparing extraction generations. It reflects the current source/chunk selection, so newly
ingested or rechunked reports may have no current facts until extraction completes.
Historical facts remain queryable through the base tables. Embeddings do not require the
pgvector extension; vector similarity indexing can be added separately if needed.

The page filter excludes short pages and pages matching financial-table or boilerplate
keywords. Stored pages are the retained text, not a full PDF transcription. The extractor
excludes ordinary financial-statement line items where possible. Chunks overlap by about 700
characters; the extraction input tells the model to extract only from `included_pages`
so overlap text is not extracted twice. Facts with source pages outside their
originating chunk are preserved with `page_in_chunk = false`, and facts whose
`source_quote` cannot be found in the chunk text are flagged `quote_in_chunk = false`
(`NULL` for rows extracted before that check existed). Warnings are recorded in
`ops.data_quality_results`. Verify flagged facts before relying on them.

Example query (company and quarter keys are compatible with financial datasets):

```sql
SELECT symbol, fiscal_year, fiscal_quarter, category, info,
       page, source_quote, page_in_chunk, quote_in_chunk
FROM gold.current_activity_report_facts
WHERE symbol = 'THYAO'
  AND model = 'gpt-6-luna'
  AND extraction_version = 'gpt6_luna_v1'
ORDER BY fiscal_year, fiscal_quarter, page;
```

## Development

Test and lint commands, including the `BIST_TEST_DSN` integration-test setup, are in the [root README](../README.md#development).

