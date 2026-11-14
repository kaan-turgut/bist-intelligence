-- Flag facts whose source_quote cannot be found in the chunk the model was given.
-- NULL means "not checked" (rows extracted before this migration).
ALTER TABLE silver.activity_report_extractions ADD COLUMN quote_in_chunk boolean;

-- New columns must be appended to keep CREATE OR REPLACE VIEW valid.
CREATE OR REPLACE VIEW gold.current_activity_report_facts AS
SELECT e.id, d.company_id, m.symbol, d.year AS fiscal_year, d.period AS fiscal_quarter,
       d.id AS document_id, d.disclosure_index, d.file_path, d.source_sha256,
       c.id AS chunk_id, c.start_page, c.end_page, c.included_pages,
       r.id AS extraction_run_id, r.pipeline_run_id, r.model, r.extraction_version, r.prompt_hash,
       e.page, e.page_in_chunk, e.category, e.event_type, e.tags, e.sentiment,
       e.magnitude, e.time_relevance, e.confidence, e.source_quote, e.info, e.created_at,
       e.quote_in_chunk
FROM silver.activity_report_extractions e
JOIN ops.activity_report_extraction_runs r ON r.id = e.extraction_run_id
JOIN silver.activity_report_chunks c ON c.id = r.chunk_id
JOIN silver.current_activity_report_documents d ON d.id = c.document_id
JOIN metadata.companies m USING (company_id)
WHERE c.is_active AND r.is_active AND r.status = 'completed';
