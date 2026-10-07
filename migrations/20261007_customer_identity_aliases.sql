-- Schema only. Apply before deploying code that reads this table.
-- Historical account links require separate evidence and explicit review.
BEGIN;
CREATE TABLE IF NOT EXISTS pagos.customer_identity_aliases (
    alias varchar(100) PRIMARY KEY,
    atlas_user_id integer NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    evidence_ref text NOT NULL CHECK (length(trim(evidence_ref)) > 0),
    verified_by text NOT NULL CHECK (length(trim(verified_by)) > 0),
    verified_at timestamptz NOT NULL DEFAULT now(),
    CHECK (alias ~ '^[1-9][0-9]*$'),
    CHECK (alias <> atlas_user_id::text)
);
CREATE INDEX IF NOT EXISTS ix_customer_identity_aliases_user
    ON pagos.customer_identity_aliases(atlas_user_id);

CREATE OR REPLACE FUNCTION pagos.validate_customer_identity_alias()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND
       (OLD.alias <> NEW.alias OR OLD.atlas_user_id <> NEW.atlas_user_id) THEN
        RAISE EXCEPTION 'Historical ownership is immutable; a new reviewed correction is required';
    END IF;
    IF EXISTS (SELECT 1 FROM public.users WHERE
        id::text = NEW.alias OR lower(trim(email)) = NEW.alias OR uuid::text = NEW.alias) THEN
        RAISE EXCEPTION 'Historical alias conflicts with a live Atlas identity';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS validate_customer_identity_alias ON pagos.customer_identity_aliases;
CREATE TRIGGER validate_customer_identity_alias
BEFORE INSERT OR UPDATE ON pagos.customer_identity_aliases
FOR EACH ROW EXECUTE FUNCTION pagos.validate_customer_identity_alias();

-- Keep direct SQL writers under the same identity/duplicate rule as the API.
CREATE OR REPLACE FUNCTION pagos.enforce_subscription_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE canonical text; matches integer;
BEGIN
    IF NEW.status <> 'ACTIVE' THEN RETURN NEW; END IF;
    SELECT count(*), min(id::text) INTO matches, canonical
      FROM public.users
     WHERE id::text = lower(trim(NEW.customer_id))
        OR uuid::text = lower(trim(NEW.customer_id))
        OR lower(trim(email)) = lower(trim(NEW.customer_id))
        OR id IN (SELECT atlas_user_id FROM pagos.customer_identity_aliases
                  WHERE alias = lower(trim(NEW.customer_id)));
    IF matches <> 1 THEN
        RAISE EXCEPTION 'Subscription customer must identify exactly one Atlas user';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('atlas-subscription:' || canonical, 0));
    IF EXISTS (
        SELECT 1 FROM pagos.recurring_payments r JOIN public.users u ON u.id::text = canonical
         WHERE r.status = 'ACTIVE' AND r.id <> NEW.id AND (
           lower(trim(r.customer_id)) IN (u.id::text, lower(trim(u.email)), u.uuid::text)
           OR lower(trim(r.customer_id)) IN
              (SELECT alias FROM pagos.customer_identity_aliases WHERE atlas_user_id = u.id))
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
COMMIT;
