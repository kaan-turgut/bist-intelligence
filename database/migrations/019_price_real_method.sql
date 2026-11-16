-- Record how each real (inflation-adjusted) close was derived.
--   cpi:                        the price month's own CPI.
--   latest_cpi_carried_forward: the price month is newer than the latest CPI month, so
--                               the latest CPI level stands in for it (no inflation is
--                               assumed after that month until its CPI is published).
ALTER TABLE gold.price_series ADD COLUMN IF NOT EXISTS close_real_method text
    CHECK (close_real_method IN ('cpi', 'latest_cpi_carried_forward'));

UPDATE gold.price_series SET close_real_method='cpi'
WHERE close_real IS NOT NULL AND close_real_method IS NULL;

-- The view's column list is fixed when it is created; recreate it to expose the new column.
CREATE OR REPLACE VIEW gold.current_price_series AS
SELECT g.* FROM gold.price_series g
JOIN ops.active_instrument_versions a ON a.dataset_name='prices'
 AND a.run_id=g.calculation_run_id AND a.instrument_id=g.instrument_id;
