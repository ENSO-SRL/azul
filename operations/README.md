# Reconciliación de la identidad de facturación

La cuenta actual puede tener otro ID que una suscripción histórica. El servicio
reconoce un ID antiguo solo después de registrar una asociación verificada;
no utiliza el correo del titular de la tarjeta para conceder acceso.

## Danilo: cuentas existentes 233 y 133

Estado del paquete del 8 de octubre: preparado y validado localmente. El usuario
confirmó el COMMIT de la migración de estructura; el código no está desplegado
y el vínculo específico sigue pendiente. La cuenta usada por WhatsApp es 133; la suscripción pagada está
en 233. La solución es una vinculación de facturación explícita 233 → 133 en
`customer_identity_links`, distinta de los alias de cuentas eliminadas. No borra
usuarios, cambia credenciales, mueve reservas ni usa `parent_id`. Autenticación
y perfiles permanecen separados; todos los accesos al servicio de pagos resuelven
la misma membresía y sus tarjetas. Una fusión global de perfiles queda fuera de
esta operación de facturación.

El período que debe conservarse termina el 7 de noviembre de 2026 a las
15:59:34.450182 UTC (11:59:34 hora dominicana). No basta con ejecutar el SQL
del vínculo sobre la versión anterior: ese lector no conoce la tabla nueva.

1. Aplicar únicamente `migrations/20261008_verified_billing_links.sql` en la
   base compartida. Crea la estructura vacía y actualiza la protección de
   suscripciones; no vincula cuentas. La migración histórica del 7 de octubre
   es un prerrequisito. El rol del servicio necesita lectura de la tabla nueva.
2. Con autorización explícita por entorno, desplegar el código de pagos en
   QA y producción y verificar la revisión en todas las instancias y workers.
   El push a esas ramas dispara despliegue; no hacerlo sin autorización.
3. Ejecutar `danilo_233_to_133_review.sql` completo: ensaya y revierte. Comprobar
   el resultado `ENSAYO_CORRECTO_SIN_APLICAR`. Si aparece un bloqueo, conservar
   el error y revisar su causa; no eliminar comprobaciones ni forzar reintentos.
4. Ejecutar `danilo_233_to_133_apply.sql` para confirmar el vínculo revisado.
   Revalida evidencia y operaciones pendientes bajo bloqueos breves; registra
   responsable y evidencia. La salida posterior al COMMIT debe indicar
   `APLICADO_Y_VERIFICADO`, origen 233, destino 133 y una suscripción activa.
   Conserva las filas de usuarios, pagos, suscripciones y tarjetas, comprobando
   sus huellas completas antes y después. No hace cargos ni amplía fechas.
5. Consultar por GET `recurring/customer-status` y `tokens/status` para 133 y
   233. Ambos deben indicar `paid`, acceso permitido, una suscripción activa
   y la vigencia exacta. El resumen de facturación devuelve ID principal 133.
   Esperar a que expire la caché negativa de Atlas; verificar su próxima
   consulta sin crear una reserva ni ejecutar un cobro de prueba.

La operación aborta si cambian las cuentas o el pago probado, hay colisiones
con terceros, otra suscripción activa, varias tarjetas predeterminadas, cobros
inciertos o activaciones pendientes. Tampoco admite cadenas de asociaciones.
Reejecutar no crea un segundo vínculo; si el estado posterior cambió, exige
otra revisión. Eliminar el vínculo no deshace operaciones realizadas después:
una reversión requiere revisar nuevos pagos y escrituras, y autorización del
entorno. No quitar las tablas mientras el código dependa de ellas.

Validación: 143 pruebas sin red y PostgreSQL 17.6 desechable. Incluye acceso
por ambos ID/correos/UUID, sesión antigua sin nuevo cobro, registro concurrente,
restricciones en SQL directo, idempotencia, reversión del ensayo y rechazo de
evidencia modificada. `tests/check_postgresql_billing_links.py` solo conecta a
127.0.0.1 con identidades y tokens ficticios. Estos resultados no equivalen a
una comprobación del cambio desplegado en producción.

El checkout también usa la misma política de vigencia al mostrar el formulario.
No confunde una fila ACTIVE con un período pagado, ni una prueba vencida con
el período pagado actual. Su dependencia de tarjetas recibe la sesión de base
de datos, necesaria para resolver los vínculos y cambiar la tarjeta principal.

## Caso revisado: cuenta actual 259, ID histórico 172

La API devuelve `payment_due` para 259 porque encuentra únicamente la suscripción
por correo cuyo período terminó el 7 de octubre de 2026. La suscripción bajo 172
tiene un cobro del 4 de octubre aprobado y conciliado. Su período calculado por
`last_charged_at + frequency_days` termina el 3 de noviembre a las 23:34:35 UTC.
El usuario 172 ya no existe. El detalle y las comprobaciones constan en los SQL
de revisión; no se incluyen secretos ni tokens en estos procedimientos.

## Secuencia para el entorno expresamente autorizado

Comprobación de ECS del 7 de octubre de 2026: `Atlas-Prueba/pago-azul-qa-svc`
y `default/pago-azul-svc` apuntan a la misma base `Atlas_User_Service`; ambos
tienen `AZUL_ENV=production`. Una modificación de datos desde QA también
afecta producción. Revalidar esta configuración antes de intervenir. No probar
cobros en QA suponiendo que usa el sandbox de Azul. La autorización debe cubrir
la base compartida y cada servicio que se despliegue; no equivale a aprobar cargos.

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
   autorizado y esperar que todas las instancias que usan la base compartida
   sirvan esa revisión, incluidos ambos servicios. Consultar
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
