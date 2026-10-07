-- ══════════════════════════════════════════════════════════════════════════════
-- BORRADO COMPLETO DE USUARIO — Atlas + Pagos
-- ══════════════════════════════════════════════════════════════════════════════
-- INSTRUCCIONES:
--   1. Cambia el número de teléfono en la línea marcada ← CAMBIAR AQUÍ
--   2. Ejecuta TODO el script
--   3. Revisa los conteos de la verificación final (todo debe ser 0)
-- ══════════════════════════════════════════════════════════════════════════════

-- ═══════════════════════════════════════════════════════════════════
-- PASO 0: Guardar IDs ANTES de borrar (persiste post-COMMIT)
-- ═══════════════════════════════════════════════════════════════════
DROP TABLE IF EXISTS _saved_ids;
CREATE TEMP TABLE _saved_ids AS
SELECT id FROM users
WHERE regexp_replace(COALESCE(phone,''), '\D', '', 'g') = '18494041395';  -- ← CAMBIAR AQUÍ (id=54, Zadkiel Duran)


BEGIN;

-- ── Números objetivo ─────────────────────────────────────────────────────────
CREATE TEMP TABLE _nums(num text) ON COMMIT DROP;
INSERT INTO _nums (SELECT regexp_replace(COALESCE(phone,''), '\D', '', 'g')
                   FROM users WHERE id IN (SELECT id FROM _saved_ids) LIMIT 1);

-- ── IDs de usuarios objetivo ─────────────────────────────────────────────────
CREATE TEMP TABLE _tu ON COMMIT DROP AS
SELECT id FROM _saved_ids;

-- ── Conjuntos derivados (FK chains) ──────────────────────────────────────────
CREATE TEMP TABLE _res ON COMMIT DROP AS
SELECT id FROM reservas WHERE user_id IN (SELECT id FROM _tu);

CREATE TEMP TABLE _hab ON COMMIT DROP AS
SELECT id FROM user_habits
WHERE regexp_replace(COALESCE(user_phone,''), '\D', '', 'g') IN (SELECT num FROM _nums);

CREATE TEMP TABLE _notif ON COMMIT DROP AS
SELECT id FROM notifications
WHERE sender_id IN (SELECT id FROM _tu)
   OR linked_reserva_id IN (SELECT id FROM _res)
   OR linked_user_habit IN (SELECT id FROM _hab);

CREATE TEMP TABLE _payments ON COMMIT DROP AS
SELECT id FROM pagos.payments
WHERE customer_id::text IN (SELECT id::text FROM _tu);


-- ═══════════════════════════════════════════════════════════════════
-- SCHEMA: public (Atlas)
-- ═══════════════════════════════════════════════════════════════════

DELETE FROM detalle_reserva
WHERE user_id IN (SELECT id FROM _tu)
   OR reserva_id IN (SELECT id FROM _res);

DELETE FROM notification_reactions
WHERE user_id IN (SELECT id FROM _tu)
   OR notification_id IN (SELECT id FROM _notif);

DELETE FROM notification_recipients
WHERE user_id IN (SELECT id FROM _tu)
   OR notification_id IN (SELECT id FROM _notif);

DELETE FROM notifications WHERE id IN (SELECT id FROM _notif);

DELETE FROM invoices WHERE user_id IN (SELECT id FROM _tu);

DELETE FROM queued_reservations
WHERE user_id IN (SELECT id FROM _tu)
   OR regexp_replace(COALESCE(user_phone,''), '\D', '', 'g') IN (SELECT num FROM _nums);

DELETE FROM reservas WHERE user_id IN (SELECT id FROM _tu);

DELETE FROM user_habits WHERE id IN (SELECT id FROM _hab);

DELETE FROM credentials                  WHERE user_id IN (SELECT id FROM _tu);
DELETE FROM interacciones_usuario        WHERE user_id IN (SELECT id FROM _tu);
DELETE FROM perfil_personalidad_usuario  WHERE user_id IN (SELECT id FROM _tu);
DELETE FROM votos_facetas_usuario        WHERE user_id IN (SELECT id FROM _tu);
DELETE FROM user_subscriptions           WHERE user_id IN (SELECT id FROM _tu);


-- ═══════════════════════════════════════════════════════════════════
-- SCHEMA: pagos (Azul Pagos Atlas)
-- ═══════════════════════════════════════════════════════════════════

DELETE FROM pagos.reconciliation_reports
WHERE payment_id IN (SELECT id FROM _payments);

DELETE FROM pagos.transactions
WHERE payment_id IN (SELECT id FROM _payments);

DELETE FROM pagos.consent_records
WHERE customer_id::text IN (SELECT id::text FROM _tu);

DELETE FROM pagos.recurring_payments
WHERE customer_id::text IN (SELECT id::text FROM _tu);

DELETE FROM pagos.saved_cards
WHERE customer_id::text IN (SELECT id::text FROM _tu);

DELETE FROM pagos.payments
WHERE customer_id::text IN (SELECT id::text FROM _tu);


-- ═══════════════════════════════════════════════════════════════════
-- Finalizar borrado de usuario
-- ═══════════════════════════════════════════════════════════════════

DELETE FROM user_invitados WHERE titular_id IN (SELECT id FROM _tu);
UPDATE users SET parent_id = NULL WHERE parent_id IN (SELECT id FROM _tu);
DELETE FROM users WHERE id IN (SELECT id FROM _tu);

COMMIT;


-- ═══════════════════════════════════════════════════════════════════
-- Verificación final (usa _saved_ids que persiste post-COMMIT)
-- Todo debe mostrar 0
-- ═══════════════════════════════════════════════════════════════════
SELECT 'public.users' AS tabla, COUNT(*) AS registros
FROM users WHERE id IN (SELECT id FROM _saved_ids)
UNION ALL
SELECT 'pagos.saved_cards', COUNT(*)
FROM pagos.saved_cards WHERE customer_id::text IN (SELECT id::text FROM _saved_ids)
UNION ALL
SELECT 'pagos.recurring_payments', COUNT(*)
FROM pagos.recurring_payments WHERE customer_id::text IN (SELECT id::text FROM _saved_ids)
UNION ALL
SELECT 'pagos.payments', COUNT(*)
FROM pagos.payments WHERE customer_id::text IN (SELECT id::text FROM _saved_ids);

DROP TABLE IF EXISTS _saved_ids;


-- ═══════════════════════════════════════════════════════════════════
-- BORRADO DIRECTO POR CUSTOMER_ID (sin depender de public.users)
-- Usar si el usuario ya fue borrado de users pero quedaron datos en pagos
-- ═══════════════════════════════════════════════════════════════════
BEGIN;
DELETE FROM pagos.reconciliation_reports WHERE payment_id IN (SELECT id FROM pagos.payments WHERE customer_id::text = '54');
DELETE FROM pagos.transactions WHERE payment_id IN (SELECT id FROM pagos.payments WHERE customer_id::text = '54');
DELETE FROM pagos.consent_records WHERE customer_id::text = '54';
DELETE FROM pagos.recurring_payments WHERE customer_id::text = '54';
DELETE FROM pagos.saved_cards WHERE customer_id::text = '54';
DELETE FROM pagos.payments WHERE customer_id::text = '54';
COMMIT;

-- Verificar que quedó limpio
SELECT 'saved_cards' AS tabla, COUNT(*) AS registros FROM pagos.saved_cards WHERE customer_id::text = '54'
UNION ALL
SELECT 'recurring_payments', COUNT(*) FROM pagos.recurring_payments WHERE customer_id::text = '54'
UNION ALL
SELECT 'payments', COUNT(*) FROM pagos.payments WHERE customer_id::text = '54';
