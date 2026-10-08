-- Apply before deploying the payment lifecycle changes. No historical rows are repaired.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='15s';
ALTER TABLE pagos.billing_attempts ADD COLUMN IF NOT EXISTS request_fingerprint varchar(64) NOT NULL DEFAULT '';
ALTER TABLE pagos.billing_attempts ADD COLUMN IF NOT EXISTS context_json text NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS ix_billing_attempt_payment ON pagos.billing_attempts(payment_id);
COMMIT;
