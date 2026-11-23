-- Prepared from the production read-only audit on 2026-11-23.
-- Requires explicit authorization before applying. No charges or scheduling changes.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '20s';
CREATE TEMP TABLE reviewed_membership_prices (
    id text PRIMARY KEY, customer_id text NOT NULL, status text NOT NULL
) ON COMMIT DROP;
INSERT INTO reviewed_membership_prices VALUES
('b102b0e1-6b6a-49bd-9608-dcae2c2cacbf','185','ACTIVE'),
('a5cc6197-7ce8-41fb-8d34-c76acb32d992','186','ACTIVE'),
('8b2e799f-70e9-44b4-8d1a-74a84b381edd','196','ACTIVE'),
('4e65d366-9222-4008-ae32-e243878d61bd','207','ACTIVE'),
('b0aa2899-fe8a-4e08-a07c-1fb7bed43d35','213','ACTIVE'),
('725d3567-6d84-4929-9d7c-c03a09940aa7','214','ACTIVE'),
('66cc5898-191e-4ad8-a679-390ea0a9347a','215','PAUSED'),
('734494ff-3abd-4e47-9b14-9267d1969973','224','ACTIVE'),
('37eca312-00b2-4005-abc3-d9cec03c8a3e','227','ACTIVE'),
('17fb0147-470b-4156-a69b-f108c6fba546','228','ACTIVE'),
('0e5f4155-b74a-4863-8c76-524d551857fe','229','ACTIVE'),
('9cfd4f93-12f6-4644-8b53-d913ce7f239a','230','ACTIVE'),
('0346decb-c12c-4943-8632-2176400142d6','231','ACTIVE'),
('ff191906-68b4-4170-a681-e94b658afc8f','232','ACTIVE'),
('3b71d6bb-316c-4d04-b87c-829c38cd0ef5','233','ACTIVE'),
('a2c5d928-045e-4076-8fd3-9987b0448040','234','ACTIVE'),
('6fbf55fe-7649-450e-b0c9-d21794c8294b','235','ACTIVE'),
('29f34951-0dbc-4494-8cec-a194ce393dc9','237','ACTIVE'),
('704b2792-24f5-4a2f-83dc-efd966c1eb81','238','ACTIVE'),
('4234fe87-d133-40b4-9388-c25c7f94306b','239','ACTIVE'),
('439efae9-a5e2-4c0e-aed2-e914883c1d1a','241','ACTIVE'),
('5c9ec5ec-885e-45bf-b439-28fc37063ec8','257','ACTIVE'),
('c364b9d5-5ba1-45c0-afb3-44ef12abf8ed','259','ACTIVE'),
('0560bf13-50d0-4460-9711-5299fb1b3560','260','ACTIVE'),
('c7874293-d3a2-442c-8003-86609d4ac956','261','PAUSED'),
('b22ec5d6-c134-46de-b2f0-35edddb350fd','262','ACTIVE'),
('13017707-2a09-4255-aeda-720d33effea9','263','ACTIVE'),
('964a45ca-5617-4cbb-b826-a64e74925b69','264','ACTIVE'),
('7e80ad6b-dd9b-4d02-b496-d497f7389bd1','265','ACTIVE'),
('b26b7f2f-1544-438f-ac07-34c08972c984','265','PAUSED'),
('e0de66b1-a6e3-4d65-92e9-465c3b696179','268','ACTIVE'),
('0b3a5975-8b0d-4144-85c5-7e71b57f010d','269','ACTIVE'),
('7f0fa7cd-5df2-47fb-bf04-143be469723e','270','ACTIVE'),
('e8506923-b86d-47f8-b83b-f158c77730ab','271','ACTIVE'),
('c273abae-ccc4-49d0-a8e1-56359dfc7292','272','ACTIVE'),
('047a43bb-0763-436f-a346-8519cc02d4fc','273','ACTIVE'),
('0fcb6b81-2ada-45e0-8f40-54131dedcc7e','275','ACTIVE'),
('5fd78ce2-0656-42d8-9502-e0142a16366c','276','ACTIVE'),
('e3168740-887d-4cac-a702-fcfede950366','277','ACTIVE'),
('3b1a1a0d-46c8-40b2-ad62-8c443f94b3ea','278','ACTIVE'),
('3f57558d-2e39-44cf-b98d-4e3330b31daa','279','ACTIVE'),
('f738ba54-dd62-4df6-9944-1183d7be8a1c','280','ACTIVE'),
('6148be86-d260-4290-84b8-4a5ab590976e','281','ACTIVE'),
('094ec8e5-53cb-4eb0-a050-95843c15c453','283','ACTIVE'),
('2f7f8126-ea06-4368-ab24-41f8be8db049','285','ACTIVE'),
('2b2b154c-48ed-4c11-a5be-1cc39539b005','286','ACTIVE'),
('f01d95ec-e1db-49e6-8ae9-6db60de87c49','289','ACTIVE'),
('ab241941-1d7e-4435-8bc0-be2f09955b0d','290','ACTIVE'),
('6d5c888d-508c-46da-8474-c66a92edfea7','291','ACTIVE'),
('2ded5f99-d17a-42ec-86d1-afd0f7fee6fb','292','ACTIVE'),
('38d5d4c6-b41d-462b-a235-f1e643cd1620','293','ACTIVE'),
('ccf30189-edef-423f-9428-5840382e7277','72','ACTIVE'),
('383a6d5a-76fd-45f2-a52d-f6b14d3b22b1','82','ACTIVE'),
('5fec1fe9-4d6a-4118-b032-9891fd241b3a','abrigoverde276@yopmail.com','ACTIVE'),
('892eeb33-b0d4-43fe-89a5-cabf7242ac49','abrigoverde277@yopmail.com','ACTIVE');
DO $pricing$
DECLARE matched integer; changed integer;
BEGIN
    PERFORM s.id FROM pagos.recurring_payments s
    JOIN reviewed_membership_prices e ON e.id=s.id FOR UPDATE OF s;
    SELECT count(*) INTO matched FROM pagos.recurring_payments s
    JOIN reviewed_membership_prices e ON e.id=s.id
    WHERE s.customer_id=e.customer_id AND s.status=e.status
      AND s.amount=59000 AND s.itbis=9000 AND s.currency_code='DOP'
      AND s.frequency_days=30;
    IF matched <> 55 THEN
        RAISE EXCEPTION 'Pricing review changed: expected 55 matching rows, got %', matched;
    END IF;
    UPDATE pagos.recurring_payments s SET amount=50000
    FROM reviewed_membership_prices e WHERE s.id=e.id;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 55 THEN
        RAISE EXCEPTION 'Unexpected update count: %', changed;
    END IF;
END $pricing$;
COMMIT;
