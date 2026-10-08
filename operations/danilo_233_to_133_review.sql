-- ENSAYO: termina en ROLLBACK. No cobra, no borra ni mueve historiales.
-- Requiere 20261008_verified_billing_links.sql y TODOS los servicios de pagos
-- que usan esta base desplegados con el lector de customer_identity_links.
-- La evidencia de identidad fue revisada en el caso Danilo del 2026-10-08.
-- No es un procedimiento general para vincular cuentas por nombre o teléfono.
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='15s';
LOCK TABLE public.users, pagos.customer_identity_aliases, pagos.customer_identity_links,
    pagos.recurring_payments, pagos.payments, pagos.saved_cards,
    pagos.billing_attempts, pagos.subscription_activation_jobs
    IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE danilo_keys ON COMMIT DROP AS
SELECT id::text AS identifier FROM public.users WHERE id IN (133,233)
UNION SELECT lower(trim(email)) FROM public.users WHERE id IN (133,233)
UNION SELECT lower(trim(uuid::text)) FROM public.users WHERE id IN (133,233)
UNION SELECT alias FROM pagos.customer_identity_aliases WHERE atlas_user_id IN (133,233);

DO $$
DECLARE matches integer;
BEGIN
    SELECT count(*) INTO matches FROM public.users c JOIN public.users s ON s.id=233
    WHERE c.id=133 AND c.name='Danilo' AND c.last_name='Bobadilla'
      AND s.name='Danilo' AND s.last_name='Bobadilla'
      AND c.created_at='2026-07-24 11:45:59.588167+00'::timestamptz
      AND s.created_at='2026-08-21 02:35:17.273336+00'::timestamptz
      AND c.is_active AND NOT s.is_active AND c.is_confirmed AND s.is_confirmed
      AND NOT c.is_cancelled AND NOT s.is_cancelled AND c.parent_id IS NULL AND s.parent_id IS NULL
      AND nullif(c.bsuid_meta,'') IS NOT NULL AND nullif(s.bsuid_meta,'') IS NULL
      AND s.phone=c.phone||'_duplicado_233'
      AND lower(trim(c.email))<>lower(trim(s.email));
    IF matches<>1 THEN RAISE EXCEPTION 'Las cuentas cambiaron; revisar identidad antes de aplicar'; END IF;
    IF EXISTS (SELECT 1 FROM public.users WHERE parent_id IN (133,233)) THEN
        RAISE EXCEPTION 'Hay invitados que no estaban en la evidencia revisada';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.customer_identity_links
        WHERE (source_user_id IN (133,233) OR atlas_user_id IN (133,233))
          AND NOT (source_user_id=233 AND atlas_user_id=133
            AND evidence_ref='case:danilo-20261008;source:233;primary:133;payment:20e0266d-fa07-41f0-b149-7d84de0e0176')) THEN
        RAISE EXCEPTION 'Existe una vinculación distinta o adicional';
    END IF;
    IF EXISTS (SELECT 1 FROM public.users WHERE id NOT IN (133,233)
      AND (id::text IN (SELECT identifier FROM danilo_keys)
        OR lower(trim(email)) IN (SELECT identifier FROM danilo_keys)
        OR uuid::text IN (SELECT identifier FROM danilo_keys)))
      OR EXISTS (SELECT 1 FROM pagos.customer_identity_aliases
          WHERE atlas_user_id NOT IN (133,233) AND alias IN (SELECT identifier FROM danilo_keys)) THEN
        RAISE EXCEPTION 'Una identidad coincide con otra cuenta';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pagos.payments
        WHERE id::text='20e0266d-fa07-41f0-b149-7d84de0e0176' AND customer_id='233'
          AND atlas_user_id IS NULL AND status='APPROVED' AND payment_type='SALE'
          AND amount=50000 AND currency='$'
          AND created_at='2026-10-08 15:59:34.450182+00'::timestamptz) THEN
        RAISE EXCEPTION 'Cambió la evidencia del pago aprobado';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pagos.recurring_payments
        WHERE id::text='3b71d6bb-316c-4d04-b87c-829c38cd0ef5' AND customer_id='233'
          AND status='ACTIVE' AND amount=50000 AND frequency_days=30
          AND last_charged_at='2026-10-08 15:59:34.450182+00'::timestamptz
          AND next_charge_at='2026-11-07 15:59:34.450182+00'::timestamptz
          AND next_charge_at>now()) THEN
        RAISE EXCEPTION 'Cambió la evidencia o venció el período de la suscripción';
    END IF;
    IF (SELECT count(*) FROM pagos.recurring_payments WHERE status='ACTIVE'
        AND lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys))<>1 THEN
        RAISE EXCEPTION 'Debe haber exactamente una suscripción activa entre ambas cuentas';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.recurring_payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)
        AND id::text NOT IN ('3b71d6bb-316c-4d04-b87c-829c38cd0ef5','69c38cd1-4886-4f79-b5a7-ae48b8a11e30')) THEN
        RAISE EXCEPTION 'Apareció otra suscripción: revisar antes de vincular';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pagos.recurring_payments
        WHERE id::text='69c38cd1-4886-4f79-b5a7-ae48b8a11e30' AND status='CANCELLED'
          AND lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)) THEN
        RAISE EXCEPTION 'Cambió la suscripción histórica cancelada';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)
        AND atlas_user_id IS NOT NULL AND atlas_user_id NOT IN (133,233)) THEN
        RAISE EXCEPTION 'Un pago tiene otro propietario explícito';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)
        AND status NOT IN ('APPROVED','DECLINED','ERROR','VOIDED','REFUNDED')) THEN
        RAISE EXCEPTION 'Hay pagos no finalizados: no aplicar ni reintentar a ciegas';
    END IF;
    IF (SELECT count(*) FROM pagos.saved_cards WHERE is_default
        AND lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys))>1 THEN
        RAISE EXCEPTION 'Hay más de una tarjeta predeterminada: requiere revisión';
    END IF;
    IF EXISTS (SELECT 1 FROM pagos.billing_attempts b WHERE b.status IN ('RESERVED','UNCERTAIN')
      AND (b.subscription_id IN ('133','233') OR b.subscription_id IN
        (SELECT id::text FROM pagos.recurring_payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys))
        OR b.payment_id IN (SELECT id::text FROM pagos.payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys))))
      OR EXISTS (SELECT 1 FROM pagos.subscription_activation_jobs j WHERE j.status<>'DONE'
        AND (lower(trim(j.customer_id)) IN (SELECT identifier FROM danilo_keys)
          OR j.payment_id IN (SELECT id::text FROM pagos.payments WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)))) THEN
        RAISE EXCEPTION 'Hay cobros o activaciones pendientes; revisar su resultado primero';
    END IF;
END;
$$;

-- Huellas completas en una tabla temporal: no se imprimen datos de tarjetas.
CREATE TEMP VIEW danilo_preserved_rows AS
SELECT 'users' AS table_name,id::text AS row_id,md5(to_jsonb(u)::text) AS fingerprint
FROM public.users u WHERE id IN (133,233)
UNION ALL SELECT 'recurring_payments',id::text,md5(to_jsonb(r)::text) FROM pagos.recurring_payments r
WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)
UNION ALL SELECT 'payments',id::text,md5(to_jsonb(p)::text) FROM pagos.payments p
WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys)
UNION ALL SELECT 'saved_cards',id::text,md5(to_jsonb(c)::text) FROM pagos.saved_cards c
WHERE lower(trim(customer_id)) IN (SELECT identifier FROM danilo_keys);
CREATE TEMP TABLE danilo_before ON COMMIT DROP AS SELECT * FROM danilo_preserved_rows;

INSERT INTO pagos.customer_identity_links(source_user_id,atlas_user_id,evidence_ref,verified_by)
VALUES (233,133,'case:danilo-20261008;source:233;primary:133;payment:20e0266d-fa07-41f0-b149-7d84de0e0176',current_user)
ON CONFLICT (source_user_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS ((SELECT * FROM danilo_before EXCEPT SELECT * FROM danilo_preserved_rows)
        UNION ALL (SELECT * FROM danilo_preserved_rows EXCEPT SELECT * FROM danilo_before)) THEN
        RAISE EXCEPTION 'Se alteraron registros que debían conservarse; se revierte todo';
    END IF;
    IF pagos.billing_primary_id('133')<>133 OR pagos.billing_primary_id('233')<>133 THEN
        RAISE EXCEPTION 'La resolución de ambas cuentas no coincide';
    END IF;
END;
$$;
SELECT jsonb_build_object('resultado','ENSAYO_CORRECTO_SIN_APLICAR','source_user_id',233,
    'atlas_user_id',133,'historial_sin_cambios',true,
    'filas_preservadas',(SELECT count(*) FROM danilo_before)) AS resultado_ensayo;
DROP VIEW danilo_preserved_rows;
ROLLBACK;
