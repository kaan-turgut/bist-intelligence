-- Actual API token usage per extraction request, for cost tracking and projections.
ALTER TABLE ops.activity_report_extraction_runs
    ADD COLUMN input_tokens integer CHECK (input_tokens >= 0),
    ADD COLUMN output_tokens integer CHECK (output_tokens >= 0);
