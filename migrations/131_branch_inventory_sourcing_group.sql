ALTER TABLE branch_inventory
    ADD COLUMN IF NOT EXISTS sourcing_group varchar(20)
    CHECK (sourcing_group IN ('store', 'external_daily', 'external_slow'));
