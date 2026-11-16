-- One project-wide CPI target month ("price level") for every inflation-adjusted value.
-- bist.financials.services.price_level owns both tables: Gold and price runs read the
-- stored month, and only `set-price-level` changes it (recalculating everything first).

CREATE TABLE IF NOT EXISTS ops.price_level (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    target_year integer NOT NULL,
    target_month smallint NOT NULL CHECK (target_month BETWEEN 1 AND 12),
    run_id bigint REFERENCES ops.pipeline_runs(run_id),
    set_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.price_level_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    previous_year integer,
    previous_month smallint CHECK (previous_month BETWEEN 1 AND 12),
    target_year integer NOT NULL,
    target_month smallint NOT NULL CHECK (target_month BETWEEN 1 AND 12),
    run_id bigint REFERENCES ops.pipeline_runs(run_id),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    changed_at timestamptz NOT NULL DEFAULT now()
);
