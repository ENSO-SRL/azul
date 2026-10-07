-- REVIEW SCRIPT: ends in ROLLBACK by default. Never included in automatic deploy.
-- After approval for the specific environment, replace the final ROLLBACK with
-- COMMIT. Requires migrations/20261007_customer_identity_aliases.sql first.
-- Keeps all payment IDs, processor references, amounts, dates and statuses intact.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
LOCK TABLE public.users, pagos.recurring_payments, pagos.payments,
    pagos.customer_identity_aliases IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE matches integer;
BEGIN
    IF EXISTS (SELECT 1 FROM public.users WHERE id = 172) THEN
        RAISE EXCEPTION 'Legacy account 172 exists; stop for a fresh ownership review';
    END IF;
    SELECT count(*) INTO matches
    FROM public.users u
    JOIN pagos.recurring_payments r ON lower(trim(r.cardholder_email)) = lower(trim(u.email))
    JOIN pagos.payments p ON p.id = 'sub-aa2a676c966b-c20261004-att0'
    WHERE u.id = 259 AND u.name = 'Roberto' AND u.last_name = 'Borda'
      AND right(regexp_replace(u.phone, '[^0-9]', '', 'g'), 4) = '0044'
      AND u.is_active AND u.is_confirmed AND u.parent_id IS NULL
      AND r.id = 'aa2a676c-966b-491b-bb4c-75396559496e'
      AND r.customer_id = '172' AND r.status = 'ACTIVE'
      AND r.frequency_days = 30
      AND r.last_charged_at = '2026-10-04 23:34:35.413169+00'::timestamptz
      AND p.customer_id = '172' AND p.atlas_user_id IS NULL
      AND p.status = 'APPROVED' AND p.payment_type = 'RECURRING'
      AND p.iso_code = '00' AND p.order_id = 'REC-16D77E9D'
      AND p.amount = 50000 AND p.itbis = 9000
      AND EXISTS (SELECT 1 FROM pagos.reconciliation_reports rr
                  WHERE rr.payment_id = p.id AND rr.status = 'OK'
                    AND rr.azul_status = 'FOUND' AND rr.azul_iso_code = '00');
    IF matches <> 1 THEN
        RAISE EXCEPTION 'Verified payment/account evidence changed; stop for fresh review';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.payments WHERE customer_id = '172'
               AND atlas_user_id IS NOT NULL AND atlas_user_id <> 259) THEN
        RAISE EXCEPTION 'Historical payments point at a different Atlas account';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.customer_identity_aliases WHERE alias = '172'
               AND atlas_user_id <> 259) THEN
        RAISE EXCEPTION 'Historical ID already assigned elsewhere';
    END IF;
    INSERT INTO pagos.customer_identity_aliases(alias,atlas_user_id,evidence_ref,verified_by)
    VALUES ('172',259,'case:roberto-20261007;payment:sub-aa2a676c966b-c20261004-att0;reconciliation:OK',current_user)
    ON CONFLICT (alias) DO NOTHING;
END;
$$;

SELECT alias, atlas_user_id, evidence_ref, verified_by, verified_at
FROM pagos.customer_identity_aliases WHERE alias = '172';
-- Review every active subscription before deciding whether to pause any of them.
SELECT r.id,r.customer_id,r.status,r.last_charged_at,r.next_charge_at
FROM pagos.recurring_payments r JOIN public.users u ON u.id = 259
WHERE lower(trim(r.customer_id)) IN (u.id::text,lower(trim(u.email)),u.uuid::text,'172');
ROLLBACK;
