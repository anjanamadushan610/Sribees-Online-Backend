BEGIN;
ALTER TABLE referral_commission_lines ADD COLUMN IF NOT EXISTS pool_rate_percentage NUMERIC(5,2);
ALTER TABLE referral_commission_lines ADD COLUMN IF NOT EXISTS pool_master_share NUMERIC;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_referral_line_pool_snapshot') THEN
        ALTER TABLE referral_commission_lines ADD CONSTRAINT ck_referral_line_pool_snapshot CHECK (
            (pool_rate_percentage IS NULL AND pool_master_share IS NULL) OR
            (pool_rate_percentage IS NOT NULL AND pool_master_share IS NOT NULL
             AND pool_rate_percentage BETWEEN 0 AND 100 AND pool_master_share BETWEEN 0 AND 1)
        );
    END IF;
END $$;
COMMIT;
