-- Richer facts: a fixed theme vocabulary, named entities and structured figures.
-- Rows extracted before this migration keep NULL themes/entities and free-form tags.
ALTER TABLE silver.activity_report_extractions
    ADD COLUMN themes jsonb CHECK (themes IS NULL OR jsonb_typeof(themes) = 'array'),
    ADD COLUMN entities jsonb CHECK (entities IS NULL OR jsonb_typeof(entities) = 'array');

CREATE TABLE silver.activity_report_fact_metrics (
    extraction_id bigint NOT NULL
        REFERENCES silver.activity_report_extractions(id) ON DELETE CASCADE,
    metric_number integer NOT NULL,
    name text NOT NULL,
    value double precision NOT NULL,
    unit text,
    period text,
    PRIMARY KEY (extraction_id, metric_number)
);
CREATE INDEX activity_report_fact_metrics_name_idx ON silver.activity_report_fact_metrics (name);

-- Embeddings of each fact's English summary, used for novelty and duplicate detection.
CREATE TABLE silver.activity_report_fact_embeddings (
    extraction_id bigint NOT NULL
        REFERENCES silver.activity_report_extractions(id) ON DELETE CASCADE,
    model text NOT NULL,
    embedding real[] NOT NULL CHECK (cardinality(embedding) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (extraction_id, model)
);

-- novelty compares a fact with the same company's previous reports:
-- new (nothing similar), updated (similar topic, different content, e.g. new figures),
-- repeated (said before), first_report (no earlier report to compare with).
-- duplicate_of points to a near-identical fact in the same report.
CREATE TABLE silver.activity_report_fact_novelty (
    extraction_id bigint PRIMARY KEY
        REFERENCES silver.activity_report_extractions(id) ON DELETE CASCADE,
    embedding_model text NOT NULL,
    novelty text NOT NULL CHECK (novelty IN ('new', 'updated', 'repeated', 'first_report')),
    max_similarity real,
    most_similar_extraction_id bigint
        REFERENCES silver.activity_report_extractions(id) ON DELETE SET NULL,
    duplicate_of bigint
        REFERENCES silver.activity_report_extractions(id) ON DELETE SET NULL,
    computed_at timestamptz NOT NULL DEFAULT now()
);

-- Automatic spot checks of facts against their source text by an LLM judge.
CREATE TABLE ops.activity_report_quality_checks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id bigint NOT NULL
        REFERENCES silver.activity_report_extractions(id) ON DELETE CASCADE,
    judge_model text NOT NULL,
    supported boolean NOT NULL,
    score smallint NOT NULL CHECK (score BETWEEN 1 AND 5),
    issues text,
    pipeline_run_id bigint REFERENCES ops.pipeline_runs(run_id),
    checked_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_id, judge_model)
);

-- New columns must be appended to keep CREATE OR REPLACE VIEW valid.
CREATE OR REPLACE VIEW gold.current_activity_report_facts AS
SELECT e.id, d.company_id, m.symbol, d.year AS fiscal_year, d.period AS fiscal_quarter,
       d.id AS document_id, d.disclosure_index, d.file_path, d.source_sha256,
       c.id AS chunk_id, c.start_page, c.end_page, c.included_pages,
       r.id AS extraction_run_id, r.pipeline_run_id, r.model, r.extraction_version, r.prompt_hash,
       e.page, e.page_in_chunk, e.category, e.event_type, e.tags, e.sentiment,
       e.magnitude, e.time_relevance, e.confidence, e.source_quote, e.info, e.created_at,
       e.quote_in_chunk, e.themes, e.entities,
       n.novelty, n.max_similarity, n.most_similar_extraction_id, n.duplicate_of
FROM silver.activity_report_extractions e
JOIN ops.activity_report_extraction_runs r ON r.id = e.extraction_run_id
JOIN silver.activity_report_chunks c ON c.id = r.chunk_id
JOIN silver.current_activity_report_documents d ON d.id = c.document_id
JOIN metadata.companies m USING (company_id)
LEFT JOIN silver.activity_report_fact_novelty n ON n.extraction_id = e.id
WHERE c.is_active AND r.is_active AND r.status = 'completed';

-- One row per stated figure, ready for time series and joins with financial data.
CREATE VIEW gold.current_activity_report_metrics AS
SELECT f.id AS extraction_id, f.company_id, f.symbol, f.fiscal_year, f.fiscal_quarter,
       f.document_id, f.model, f.extraction_version,
       x.metric_number, x.name AS metric_name, x.value, x.unit, x.period AS metric_period,
       f.category, f.themes, f.page, f.info, f.source_quote, f.novelty, f.duplicate_of
FROM silver.activity_report_fact_metrics x
JOIN gold.current_activity_report_facts f ON f.id = x.extraction_id;
