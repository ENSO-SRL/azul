-- ============================================================
-- Migration: Add currency_code to pagos.recurring_payments
-- Run once on Atlas_User_Service DB before redeploying the app
-- ============================================================

ALTER TABLE pagos.recurring_payments
    ADD COLUMN IF NOT EXISTS currency_code VARCHAR(3) NOT NULL DEFAULT 'DOP';

-- Optional: update existing rows from USD subscriptions if known
-- UPDATE pagos.recurring_payments SET currency_code = 'USD' WHERE id IN (...);
