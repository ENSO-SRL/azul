-- Second reviewed step, after the 172 -> 259 association and explicit approval.
-- Ends in ROLLBACK by default. A pause never calls Azul or deletes a card/token.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
LOCK TABLE public.users, pagos.recurring_payments, pagos.payments,
    pagos.customer_identity_aliases, pagos.billing_attempts IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE duplicate pagos.recurring_payments%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pagos.customer_identity_aliases WHERE alias='172' AND atlas_user_id=259) THEN
        RAISE EXCEPTION 'Verified historical association is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pagos.recurring_payments r JOIN public.users u
                   ON u.id=259 AND lower(trim(r.cardholder_email))=lower(trim(u.email))
                   WHERE r.id='aa2a676c-966b-491b-bb4c-75396559496e' AND r.customer_id='172'
                     AND r.status='ACTIVE' AND r.frequency_days=30
                     AND r.last_charged_at='2026-10-04 23:34:35.413169+00'::timestamptz
                     AND r.last_charged_at+interval '30 days'>now()) THEN
        RAISE EXCEPTION 'Replacement paid membership changed or expired';
    END IF;
    SELECT * INTO STRICT duplicate FROM pagos.recurring_payments
    WHERE id='b2fbfdd2-ddae-4ffb-ac79-d7eaea65b3c5';
    IF duplicate.amount<>200 OR duplicate.itbis<>36 OR duplicate.frequency_days<>30
       OR duplicate.last_charged_at IS DISTINCT FROM '2026-09-07 15:26:28.886629+00'::timestamptz
       OR duplicate.last_charged_at+interval '30 days'>now()
       OR duplicate.status NOT IN ('ACTIVE','PAUSED')
       OR lower(trim(duplicate.customer_id)) IS DISTINCT FROM
          (SELECT lower(trim(email)) FROM public.users WHERE id=259) THEN
        RAISE EXCEPTION 'Duplicate subscription evidence changed';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.billing_attempts WHERE subscription_id=duplicate.id
               AND status IN ('RESERVED','UNCERTAIN')) THEN
        RAISE EXCEPTION 'An unresolved billing attempt requires reconciliation before pausing';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.payments WHERE customer_id=duplicate.customer_id
               AND payment_type='RECURRING' AND created_at>duplicate.last_charged_at) THEN
        RAISE EXCEPTION 'A newer billing attempt exists; stop for fresh review';
    END IF;
    UPDATE pagos.recurring_payments SET status='PAUSED' WHERE id=duplicate.id AND status='ACTIVE';
    UPDATE pagos.customer_identity_aliases
       SET evidence_ref=evidence_ref || ';paused-expired-duplicate:b2fbfdd2-ddae-4ffb-ac79-d7eaea65b3c5'
     WHERE alias='172' AND evidence_ref NOT LIKE '%paused-expired-duplicate:%';
END;
$$;
SELECT id,customer_id,status,last_charged_at,next_charge_at
FROM pagos.recurring_payments
WHERE id IN ('aa2a676c-966b-491b-bb4c-75396559496e','b2fbfdd2-ddae-4ffb-ac79-d7eaea65b3c5');
ROLLBACK;
