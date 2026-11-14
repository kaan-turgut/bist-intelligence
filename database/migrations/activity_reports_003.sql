CREATE TABLE metadata.activity_report_disclosures (
    disclosure_index bigint PRIMARY KEY CHECK (disclosure_index > 0),
    company_id bigint REFERENCES metadata.companies(company_id),
    source_stock_codes text NOT NULL,
    fiscal_year integer,
    fiscal_quarter smallint CHECK (fiscal_quarter BETWEEN 1 AND 4),
    published_at timestamptz,
    subject text,
    summary text,
    download_url text,
    source_payload jsonb NOT NULL CHECK (jsonb_typeof(source_payload) = 'object'),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_report_disclosures_company_period_idx
    ON metadata.activity_report_disclosures (company_id, fiscal_year, fiscal_quarter);
CREATE INDEX activity_report_disclosures_published_idx
    ON metadata.activity_report_disclosures (published_at);
COMMENT ON TABLE metadata.activity_report_disclosures IS
    'KAP disclosure catalog, independent of PDF ingestion. Unmapped historical issuers retain their original codes and payload.';

CREATE TABLE ops.activity_report_downloads (
    disclosure_index bigint PRIMARY KEY
        REFERENCES metadata.activity_report_disclosures(disclosure_index) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'downloading', 'downloaded', 'failed')),
    file_path text,
    source_sha256 text CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    file_size_bytes bigint CHECK (file_size_bytes > 0),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at timestamptz,
    downloaded_at timestamptz,
    error_text text,
    pipeline_run_id bigint REFERENCES ops.pipeline_runs(run_id),
    CHECK (status <> 'downloaded' OR
           (file_path IS NOT NULL AND source_sha256 IS NOT NULL AND
            file_size_bytes IS NOT NULL AND downloaded_at IS NOT NULL))
);
CREATE INDEX activity_report_downloads_status_idx ON ops.activity_report_downloads (status);

CREATE TABLE ops.activity_report_index_searches (
    company_id bigint NOT NULL REFERENCES metadata.companies(company_id),
    query_version text NOT NULL,
    begin_date date NOT NULL,
    end_date date NOT NULL CHECK (end_date >= begin_date),
    completed_at timestamptz NOT NULL DEFAULT now(),
    pipeline_run_id bigint NOT NULL REFERENCES ops.pipeline_runs(run_id),
    PRIMARY KEY (company_id, query_version, begin_date, end_date)
);
COMMENT ON TABLE ops.activity_report_index_searches IS
    'Successfully persisted search coverage by issuer and KAP query contract. The current day is never checkpointed as complete. A disclosure list alone does not prove coverage.';
