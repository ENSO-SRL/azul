-- Run before deploying the application. Does not select/cancel historical records.
BEGIN;
ALTER TABLE pagos.recurring_payments ADD COLUMN IF NOT EXISTS method_updated_at timestamptz;
CREATE TABLE IF NOT EXISTS pagos.billing_attempts (
    id varchar(128) PRIMARY KEY,
    subscription_id varchar(100) NOT NULL,
    payment_id varchar(36) NOT NULL DEFAULT '',
    status varchar(16) NOT NULL DEFAULT 'RESERVED',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_billing_attempts_subscription_id ON pagos.billing_attempts(subscription_id);
CREATE TABLE IF NOT EXISTS pagos.subscription_activation_jobs (
    payment_id varchar(36) PRIMARY KEY,
    customer_id varchar(100) NOT NULL,
    card_expiration varchar(6) NOT NULL DEFAULT '',
    promo_code varchar(100) NOT NULL DEFAULT '',
    user_name varchar(255) NOT NULL DEFAULT '',
    status varchar(16) NOT NULL DEFAULT 'PENDING',
    last_error varchar(255) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION pagos.enforce_subscription_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE canonical text; matches integer;
BEGIN
    -- An unidentified legacy record may be closed without inventing an owner.
    IF NEW.status <> 'ACTIVE' THEN RETURN NEW; END IF;
    SELECT count(*), min(id::text) INTO matches, canonical
      FROM public.users
     WHERE id::text = lower(trim(NEW.customer_id))
        OR uuid::text = lower(trim(NEW.customer_id))
        OR lower(trim(email)) = lower(trim(NEW.customer_id));
    IF matches <> 1 THEN
        RAISE EXCEPTION 'Subscription customer must identify exactly one Atlas user';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('atlas-subscription:' || canonical, 0));
    IF EXISTS (
        SELECT 1 FROM pagos.recurring_payments r JOIN public.users u ON u.id::text = canonical
         WHERE r.status = 'ACTIVE' AND r.id <> NEW.id
           AND lower(trim(r.customer_id)) IN (u.id::text, lower(trim(u.email)), u.uuid::text)
    ) THEN
        RAISE EXCEPTION 'Atlas user already has an active subscription';
    END IF;
    NEW.customer_id := canonical;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS enforce_subscription_identity ON pagos.recurring_payments;
CREATE TRIGGER enforce_subscription_identity
BEFORE INSERT OR UPDATE OF customer_id, status ON pagos.recurring_payments
FOR EACH ROW EXECUTE FUNCTION pagos.enforce_subscription_identity();
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sub_per_customer
ON pagos.recurring_payments(customer_id) WHERE status = 'ACTIVE';
COMMIT;
