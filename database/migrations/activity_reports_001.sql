-- Extend the existing bist_finance database; shared metadata/financial tables are untouched.
CREATE TABLE bronze.activity_report_documents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id bigint NOT NULL REFERENCES metadata.companies(company_id),
    year integer NOT NULL,
    period smallint NOT NULL CHECK (period BETWEEN 1 AND 4),
    disclosure_index bigint,
    file_path text NOT NULL,
    source_sha256 text NOT NULL CHECK (length(source_sha256) = 64),
    imported_at timestamptz NOT NULL DEFAULT now(),
    run_id bigint REFERENCES ops.pipeline_runs(run_id),
    UNIQUE (company_id, year, period, source_sha256)
);
CREATE INDEX activity_report_documents_period_idx
    ON bronze.activity_report_documents (company_id, year, period);

-- Retained PDF pages, with the original PDF page numbers (not printed page labels).
CREATE TABLE bronze.activity_report_pages (
    document_id bigint NOT NULL REFERENCES bronze.activity_report_documents(id) ON DELETE CASCADE,
    page integer NOT NULL CHECK (page > 0),
    text text NOT NULL,
    char_count integer NOT NULL CHECK (char_count >= 0),
    PRIMARY KEY (document_id, page)
);

CREATE TABLE silver.activity_report_chunks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES bronze.activity_report_documents(id) ON DELETE CASCADE,
    start_page integer NOT NULL CHECK (start_page > 0),
    end_page integer NOT NULL CHECK (end_page >= start_page),
    included_pages integer[] NOT NULL,
    text text NOT NULL,
    text_hash text NOT NULL CHECK (length(text_hash) = 64),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, text_hash, start_page, end_page)
);
CREATE INDEX activity_report_chunks_document_idx ON silver.activity_report_chunks (document_id, is_active);

CREATE TABLE ops.activity_report_extraction_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id bigint NOT NULL REFERENCES silver.activity_report_chunks(id) ON DELETE CASCADE,
    pipeline_run_id bigint REFERENCES ops.pipeline_runs(run_id),
    model text NOT NULL,
    extraction_version text NOT NULL,
    prompt_hash text NOT NULL,
    extraction_count integer NOT NULL DEFAULT 0 CHECK (extraction_count >= 0),
    status text NOT NULL CHECK (status IN ('completed', 'failed')),
    error text,
    is_active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model, extraction_version, prompt_hash)
);
CREATE UNIQUE INDEX activity_report_extraction_runs_active_idx
    ON ops.activity_report_extraction_runs (chunk_id, model, extraction_version)
    WHERE is_active;

CREATE TABLE silver.activity_report_extractions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_run_id bigint NOT NULL REFERENCES ops.activity_report_extraction_runs(id) ON DELETE CASCADE,
    fact_number integer NOT NULL,
    page integer,
    page_in_chunk boolean NOT NULL,
    category text,
    event_type text,
    tags jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tags) = 'array'),
    sentiment text,
    magnitude integer CHECK (magnitude BETWEEN 1 AND 10),
    time_relevance text,
    confidence double precision CHECK (confidence BETWEEN 0 AND 1),
    source_quote text,
    info text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_run_id, fact_number)
);
CREATE INDEX activity_report_extractions_category_idx ON silver.activity_report_extractions (category, event_type);

CREATE TABLE silver.activity_report_embeddings (
    chunk_id bigint NOT NULL REFERENCES silver.activity_report_chunks(id) ON DELETE CASCADE,
    model text NOT NULL,
    embedding double precision[] NOT NULL CHECK (cardinality(embedding) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model)
);

-- A successful import is recorded in the same transaction as all imported rows.
CREATE TABLE ops.activity_report_legacy_imports (
    source_sha256 text PRIMARY KEY,
    source_path text NOT NULL,
    pipeline_run_id bigint NOT NULL REFERENCES ops.pipeline_runs(run_id),
    row_counts jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Multiple model/version combinations are deliberately exposed; callers can filter them.
CREATE VIEW silver.current_activity_report_documents AS
SELECT DISTINCT ON (company_id, year, period) *
FROM bronze.activity_report_documents
ORDER BY company_id, year, period, disclosure_index DESC NULLS LAST, id DESC;

CREATE VIEW gold.current_activity_report_facts AS
SELECT e.id, d.company_id, m.symbol, d.year AS fiscal_year, d.period AS fiscal_quarter,
       d.id AS document_id, d.disclosure_index, d.file_path, d.source_sha256,
       c.id AS chunk_id, c.start_page, c.end_page, c.included_pages,
       r.id AS extraction_run_id, r.pipeline_run_id, r.model, r.extraction_version, r.prompt_hash,
       e.page, e.page_in_chunk, e.category, e.event_type, e.tags, e.sentiment,
       e.magnitude, e.time_relevance, e.confidence, e.source_quote, e.info, e.created_at
FROM silver.activity_report_extractions e
JOIN ops.activity_report_extraction_runs r ON r.id = e.extraction_run_id
JOIN silver.activity_report_chunks c ON c.id = r.chunk_id
JOIN silver.current_activity_report_documents d ON d.id = c.document_id
JOIN metadata.companies m USING (company_id)
WHERE c.is_active AND r.is_active AND r.status = 'completed';
