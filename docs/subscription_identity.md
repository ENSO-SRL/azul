# Una suscripción activa por usuario de Atlas

El registro y el checkout ahora resuelven la identidad usando `public.users`.
El `customer_id` que se guarda en nuevas suscripciones es siempre `users.id`
convertido a texto. La búsqueda reconoce el ID, el UUID y el correo de esa
misma cuenta, incluyendo correos antiguos con espacios o mayúsculas.
El correo escrito para la tarjeta sirve para notificaciones; no se usa para
relacionar la cuenta con una suscripción.

Los IDs numéricos de cuentas antiguas eliminadas solo se reconocen mediante
`pagos.customer_identity_aliases`. Cada vínculo requiere una cuenta actual,
evidencia revisada, responsable y fecha; no se deduce del correo de la tarjeta.
El mismo resolvedor se usa en `customer-status`, el resumen web, checkout y
cobros. Un alias que colisiona con otra cuenta impide resolver la identidad.

Las cuentas duplicadas que todavía existen se vinculan por separado mediante
`pagos.customer_identity_links`, con responsable, fecha y evidencia revisada.
Ambos ID, correos y UUID resuelven la misma identidad de facturación. Esto no
fusiona perfiles, credenciales ni reservas: el vínculo solo autoriza compartir
la membresía y sus medios de pago entre las cuentas verificadas. No se deduce
una asociación de un nombre, un teléfono marcado como duplicado o un correo de
tarjeta. Los alias históricos siguen rechazando IDs de cuentas existentes.

Registro, checkout, tarjetas y consultas usan esa identidad común. Las nuevas
operaciones usan el ID principal y reutilizan la suscripción existente. Consultar
el estado conserva los identificadores históricos originales. Una sesión firmada
con el ID anterior sigue resolviendo la misma membresía. Sin sesión válida, el
checkout no acepta el `customer_id` enviado en el formulario.

## Comportamiento

1. `POST /api/v1/registration/trial` mantiene su payload actual por correo.
   Debe llamarse después de crear y confirmar la transacción del usuario en
   Atlas. Si la cuenta no existe o es ambigua, responde `409`.
2. Antes de crear una suscripción, registro y post-pago adquieren un bloqueo
   de PostgreSQL por usuario (`pg_advisory_xact_lock`). El bloqueo se libera
   al confirmar o revertir la transacción y funciona entre procesos/workers.
3. Si existe una suscripción activa guardada por correo o UUID, se reutiliza
   y se cambia su `customer_id` al ID. Se conserva el ID de la suscripción,
   la fecha de vencimiento de la prueba y el próximo cobro.
4. Si esa suscripción no tiene tarjeta, se le agrega el token y los datos de
   la tarjeta. No se crea otro registro ni se otorga otra prueba/promo.
5. Una prueba vigente usa tokenización o Hold+Void al agregar tarjeta. Si
   ambos mecanismos fallan, responde un error; no cae a un Sale de membresía.
6. Si la prueba sin tarjeta venció y se realizó un Sale aprobado, se actualiza
   esa misma suscripción y se programa el siguiente ciclo desde ese pago,
   evitando que el scheduler intente cobrar de nuevo la fecha vencida.
7. Las consultas de estado por ID, correo o UUID siguen encontrando los datos
   después de cambiar las suscripciones al ID.
8. Si ya hay dos suscripciones activas para la misma cuenta, se bloquea el
   checkout antes del cobro. No se cancelan ni se eliminan filas automáticamente.
   Los registros ya duplicados deben revisarse según sus pagos y fechas.
9. Si falla la consulta de identidad, se revierte la transacción y no se
   decide cobrar por defecto ni se crea una suscripción con otro identificador.

## Revisar duplicados existentes

Esta consulta es solo de lectura. No ejecutarla como una migración de datos:

```sql
SELECT u.id AS atlas_user_id,
       u.email,
       count(*) AS active_subscriptions,
       array_agg(r.id) AS subscription_ids
FROM public.users AS u
JOIN pagos.recurring_payments AS r
  ON lower(trim(r.customer_id)) IN
     (u.id::text, lower(trim(u.email)), u.uuid::text)
WHERE r.status = 'ACTIVE'
GROUP BY u.id, u.email
HAVING count(*) > 1;
```

## Verificación y publicación

Las pruebas de regresión están en `tests/test_subscription_identity.py`.
Usan almacenamiento ORM aislado en SQLite; traducen los casts de PostgreSQL
y simulan el bloqueo para verificar las carreras de registro/checkout.
No realizan cobros ni consultan servicios de producción.

```powershell
python -m pytest tests/test_subscription_identity.py -q
```

Antes de desplegar la resolución histórica, aplicar
`migrations/20261007_customer_identity_aliases.sql` en el entorno autorizado.
La aplicación no crea asociaciones automáticamente y debe tener permiso de
lectura sobre esa tabla. Una tabla ausente es un fallo operativo, nunca prueba
de deuda. La migración conserva las filas históricas y amplía la restricción de
suscripciones activas para reconocer los alias revisados.

El lector de cuentas existentes requiere además
`migrations/20261008_verified_billing_links.sql`, que inicialmente deja la tabla
vacía. No aplicar un vínculo hasta que todos los servicios de pagos que usan
la base compartida ejecuten esta versión. El caso Danilo está descrito en
`operations/README.md`; sus scripts no forman parte del despliegue automático.

Los procedimientos específicos de reconciliación están en `operations/`;
terminan en `ROLLBACK` y no se ejecutan desde el despliegue. Revisar las
condiciones y autorizar el entorno antes de confirmar cada operación. Una
asociación puede descubrir duplicados: el período ya pagado sigue válido,
`requires_review=true` hace visible el conflicto y se bloquean nuevos cobros
hasta revisar las suscripciones. Pausar una duplicada es una operación separada;
nunca debe hacerse cancelando o eliminando su token de tarjeta automáticamente.

La suite sin red se ejecuta con `python scripts/run_offline_tests.py`. Excluye
`test_gateway.py` y `test_sandbox_integration.py`, que llaman al procesador real.
`tests/check_postgresql_identity_migration.py` comprueba además la migración y
los procedimientos sobre una base desechable PostgreSQL local, con identidades
y tarjetas ficticias y un reloj fijo para el caso reproducido.

Los cambios locales deben revisarse y publicarse mediante el flujo existente
del repositorio. El workflow `.github/workflows/deploy-ecr.yml` se ejecuta al
enviar cambios de código a `main`, `qa` o `QA`. Este ajuste no despliega ni
modifica datos de producción por sí solo.
