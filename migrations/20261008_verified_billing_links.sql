-- Schema only: no account is linked by this migration.
-- Deploy all payment-service instances before applying a reviewed link.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '15s';
CREATE TABLE IF NOT EXISTS pagos.customer_identity_links (
    source_user_id integer PRIMARY KEY REFERENCES public.users(id) ON DELETE RESTRICT,
    atlas_user_id integer NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    evidence_ref text NOT NULL CHECK (length(trim(evidence_ref)) > 0),
    verified_by text NOT NULL CHECK (length(trim(verified_by)) > 0),
    verified_at timestamptz NOT NULL DEFAULT now(),
    CHECK (source_user_id <> atlas_user_id)
);
CREATE INDEX IF NOT EXISTS ix_customer_identity_links_target
    ON pagos.customer_identity_links(atlas_user_id);

-- This is billing ownership only, not an authentication or profile merge.
CREATE OR REPLACE FUNCTION pagos.validate_customer_identity_link()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('atlas-billing-link-review', 0));
    IF TG_OP = 'UPDATE' AND (OLD.source_user_id <> NEW.source_user_id
        OR OLD.atlas_user_id <> NEW.atlas_user_id) THEN
        RAISE EXCEPTION 'Reviewed billing ownership is immutable';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.customer_identity_links
               WHERE source_user_id = NEW.atlas_user_id OR atlas_user_id = NEW.source_user_id) THEN
        RAISE EXCEPTION 'Billing identity chains and cycles are forbidden';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.users s JOIN public.users c ON c.id=NEW.atlas_user_id
                   WHERE s.id=NEW.source_user_id AND NOT s.is_active
                     AND s.is_confirmed AND NOT s.is_cancelled AND s.parent_id IS NULL
                     AND c.is_active AND c.is_confirmed AND NOT c.is_cancelled AND c.parent_id IS NULL) THEN
        RAISE EXCEPTION 'Billing link requires a reviewed inactive source and an eligible primary account';
    END IF;
    IF (SELECT count(*) FROM pagos.recurring_payments WHERE status='ACTIVE' AND lower(trim(customer_id)) IN (
            SELECT identifier FROM pagos.billing_identity_keys(NEW.atlas_user_id)
            UNION SELECT identifier FROM pagos.billing_identity_keys(NEW.source_user_id)))>1 THEN
        RAISE EXCEPTION 'Review multiple active subscriptions before linking billing identities';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS validate_customer_identity_link ON pagos.customer_identity_links;
CREATE TRIGGER validate_customer_identity_link BEFORE INSERT OR UPDATE
ON pagos.customer_identity_links FOR EACH ROW EXECUTE FUNCTION pagos.validate_customer_identity_link();

CREATE OR REPLACE FUNCTION pagos.billing_identity_keys(primary_id integer)
RETURNS TABLE(identifier text) LANGUAGE sql STABLE AS $$
    WITH members AS (
        SELECT u.id, u.uuid, u.email FROM public.users u
        WHERE u.id = primary_id OR u.id IN
            (SELECT source_user_id FROM pagos.customer_identity_links WHERE atlas_user_id = primary_id)
    )
    SELECT id::text FROM members
    UNION SELECT lower(trim(uuid::text)) FROM members WHERE uuid IS NOT NULL
    UNION SELECT lower(trim(email)) FROM members WHERE nullif(trim(email),'') IS NOT NULL
    UNION SELECT alias FROM pagos.customer_identity_aliases WHERE atlas_user_id IN (SELECT id FROM members);
$$;

CREATE OR REPLACE FUNCTION pagos.billing_primary_id(identifier text)
RETURNS integer LANGUAGE plpgsql STABLE AS $$
DECLARE owners integer; primary_id integer;
BEGIN
    SELECT count(DISTINCT coalesce(l.atlas_user_id,u.id)), min(coalesce(l.atlas_user_id,u.id))
      INTO owners, primary_id
      FROM public.users u LEFT JOIN pagos.customer_identity_links l ON l.source_user_id=u.id
     WHERE u.id::text=lower(trim(identifier)) OR u.uuid::text=lower(trim(identifier))
        OR lower(trim(u.email))=lower(trim(identifier))
        OR u.id IN (SELECT atlas_user_id FROM pagos.customer_identity_aliases a WHERE a.alias=lower(trim(identifier)));
    IF owners <> 1 THEN RAISE EXCEPTION 'Billing identity is missing or ambiguous'; END IF;
    IF EXISTS (SELECT 1 FROM pagos.customer_identity_links WHERE source_user_id=primary_id) THEN
        RAISE EXCEPTION 'Billing identity contains a chain';
    END IF;
    IF EXISTS (SELECT 1 FROM public.users u WHERE u.id<>primary_id
        AND u.id NOT IN (SELECT source_user_id FROM pagos.customer_identity_links WHERE atlas_user_id=primary_id)
        AND (u.id::text IN (SELECT k.identifier FROM pagos.billing_identity_keys(primary_id) k)
          OR lower(trim(u.email)) IN (SELECT k.identifier FROM pagos.billing_identity_keys(primary_id) k)
          OR u.uuid::text IN (SELECT k.identifier FROM pagos.billing_identity_keys(primary_id) k)))
      OR EXISTS (SELECT 1 FROM pagos.customer_identity_aliases a WHERE a.atlas_user_id<>primary_id
        AND a.atlas_user_id NOT IN (SELECT source_user_id FROM pagos.customer_identity_links WHERE atlas_user_id=primary_id)
        AND a.alias IN (SELECT k.identifier FROM pagos.billing_identity_keys(primary_id) k)) THEN
        RAISE EXCEPTION 'Billing identity keys conflict with another owner';
    END IF;
    RETURN primary_id;
END;
$$;

CREATE OR REPLACE FUNCTION pagos.enforce_subscription_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE canonical integer;
BEGIN
    IF NEW.status <> 'ACTIVE' THEN RETURN NEW; END IF;
    canonical := pagos.billing_primary_id(NEW.customer_id);
    PERFORM pg_advisory_xact_lock(hashtextextended('atlas-subscription:' || canonical::text, 0));
    IF EXISTS (SELECT 1 FROM pagos.recurring_payments r WHERE r.status='ACTIVE' AND r.id<>NEW.id
        AND lower(trim(r.customer_id)) IN (SELECT identifier FROM pagos.billing_identity_keys(canonical))) THEN
        RAISE EXCEPTION 'Atlas billing identity already has an active subscription';
    END IF;
    NEW.customer_id := canonical::text;
    RETURN NEW;
END;
$$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sub_per_customer
    ON pagos.recurring_payments(customer_id) WHERE status='ACTIVE';
-- Recreate explicitly so a missing prior trigger cannot leave SQL writers unguarded.
DROP TRIGGER IF EXISTS enforce_subscription_identity ON pagos.recurring_payments;
CREATE TRIGGER enforce_subscription_identity BEFORE INSERT OR UPDATE OF customer_id,status
ON pagos.recurring_payments FOR EACH ROW EXECUTE FUNCTION pagos.enforce_subscription_identity();
COMMIT;
