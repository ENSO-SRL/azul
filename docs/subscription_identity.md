# Una suscripción activa por usuario de Atlas

El registro y el checkout ahora resuelven la identidad usando `public.users`.
El `customer_id` que se guarda en nuevas suscripciones es siempre `users.id`
convertido a texto. La búsqueda reconoce el ID, el UUID y el correo de esa
misma cuenta, incluyendo correos antiguos con espacios o mayúsculas.
El correo escrito para la tarjeta sirve para notificaciones; no se usa para
relacionar la cuenta con una suscripción.

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

No se requiere cambiar el esquema de base de datos. La protección de unicidad
por `customer_id` sigue definida en el modelo y el código unifica la identidad
antes de usarla. Se requiere que el servicio tenga acceso a `public.users`,
como ya lo usa el endpoint de estado.

Los cambios locales deben revisarse y publicarse mediante el flujo existente
del repositorio. El workflow `.github/workflows/deploy-ecr.yml` se ejecuta al
enviar cambios de código a `main`, `qa` o `QA`. Este ajuste no despliega ni
modifica datos de producción por sí solo.
