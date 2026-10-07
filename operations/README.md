# Reconciliación de la identidad de facturación

La cuenta actual puede tener otro ID que una suscripción histórica. El servicio
reconoce un ID antiguo solo después de registrar una asociación verificada;
no utiliza el correo del titular de la tarjeta para conceder acceso.

## Caso revisado: cuenta actual 259, ID histórico 172

La API devuelve `payment_due` para 259 porque encuentra únicamente la suscripción
por correo cuyo período terminó el 7 de octubre de 2026. La suscripción bajo 172
tiene un cobro del 4 de octubre aprobado y conciliado. Su período calculado por
`last_charged_at + frequency_days` termina el 3 de noviembre a las 23:34:35 UTC.
El usuario 172 ya no existe. El detalle y las comprobaciones constan en los SQL
de revisión; no se incluyen secretos ni tokens en estos procedimientos.

## Secuencia para el entorno expresamente autorizado

1. Confirmar el destino: QA y producción requieren autorizaciones distintas.
   Verificar si comparten base antes de ejecutar cambios de datos.
2. Guardar las filas implicadas en una copia restringida y verificar que no haya
   intentos de cobro en curso. No iniciar ningún cobro para comprobar el arreglo.
3. Aplicar `migrations/20261007_customer_identity_aliases.sql` antes del código.
   La migración es idempotente y no crea ninguna asociación de cliente.
4. Revisar `roberto_172_to_259_review.sql`. Ejecutarlo sin cambios ensaya la
   operación y revierte. Solo después de aprobarla, cambiar su último
   `ROLLBACK` por `COMMIT` en la copia de ejecución. El vínculo conserva las
   suscripciones, los pagos y las referencias del procesador tal como están.
5. Desplegar la versión revisada del servicio de pagos mediante el mecanismo
   autorizado y esperar que todas las instancias sirvan esa revisión. Consultar
   el estado: debe reconocer el pago y marcar `requires_review=true`. Con ambos
   registros activos, esta versión bloquea nuevos cobros por duplicidad. Antes
   de intervenir, comprobar que no queden cobros en vuelo de la revisión anterior;
   cualquier pausa operativa adicional requiere autorización del entorno.
6. Revisar y confirmar separadamente
   `roberto_pause_expired_duplicate_review.sql`. Solo pausa la duplicada
   vencida si el período pagado sigue vigente y no hay intentos posteriores
   o inciertos. No elimina ni revoca tokens, no reembolsa ni cobra.
   Un push/merge a `main`, `qa` o `QA` dispara el despliegue y también exige
   autorización explícita; abrir una PR no publica la imagen ni despliega.
7. Consultar `GET /api/v1/recurring/customer-status?customer_id=259` con la clave
   del entorno, sin imprimirla. Debe devolver `allow_access=true`, `reason=paid`,
   la vigencia calculada y una sola suscripción activa tras la pausa.
   El resumen web debe concordar. Si las fechas cambian, revisar evidencia;
   nunca escribir `allow_access` ni prorrogar fechas a mano.
8. La caché negativa de Atlas dura 30 segundos: esperar su expiración o
   invalidar solo las claves de pago de 259 en el entorno autorizado. Comprobar
   su próxima solicitud VV Autos sin enviar una reserva real como prueba.

## Reversión

El despliegue y su reversión también necesitan autorización. Conservar la tabla
de alias al revertir el binario: borrarla mientras el nuevo código esté activo
causaría fallos de identidad. Para deshacer el vínculo, guardar su evidencia y
eliminar únicamente `alias='172' AND atlas_user_id=259`; esto vuelve al estado
de acceso anterior. No reactivar automáticamente la duplicada: podría habilitar
un nuevo cobro. Cualquier restauración de estado exige revisar los intentos
desde la intervención. Los pagos aprobados nunca se borran o cambian de estado.

## Verificación realizada

- Suite aislada: 114 pruebas aprobadas, sin red ni credenciales reales.
- PostgreSQL 17 local: migración y asociación idempotentes, ensayo que revierte,
  validación de evidencia, rechazo de conflictos y duplicados, pausa sin
  pérdida de historial ni tokens y acceso hasta el período pagado real.
- El contrato resultante se validó con el normalizador de pagos de Atlas.
- Las pruebas de integración con Azul real requieren otra autorización y no
  forman parte de esta validación.

Estos resultados no significan que el cambio ya esté aplicado en QA o producción.
