# Facturación de suscripciones y checkout

Cada cobro aprobado de Atlas guarda una factura inmutable en `pagos.invoices`.
Los importes son centavos enteros; subtotal = total aprobado - ITBIS aprobado.
El número `INV-ATLAS-año-secuencia` es interno. No se fabrica un NCF.
El emisor y diseño son los del modelo de Colina del Sol autorizado por el usuario.
La metadata y los trabajos se guardan junto al pago. HTML/PDF se generan después,
en otra transacción, y se reintentan si falla el renderizador. Un fallo al crear
el documento no revierte ni convierte en incierto un cargo bancario aprobado.
Las tarjetas, verificación sin cobro, rechazos, devoluciones y servicios cobrados
por cuenta de terceros no generan una nueva factura de suscripción.

La factura contiene fecha original en Santo Domingo, cliente, concepto, método
con últimos cuatro dígitos, total pagado y saldo pendiente cero. Se conservan
JSON, HTML, PDF y SHA-256 del PDF. No contiene PAN completo, CVV ni token bancario.
No se modifica la tabla `public.invoices`, que corresponde a reservas.

## Destinatarios

- Cliente: correo del cobro.
- Contabilidad: Daty Loritza Monegro Cuello, `dmonegro@enso.do`.

La copia contable es un correo independiente con el mismo PDF, no un CC visible
al cliente. Si coinciden las direcciones, se guarda un solo envío. El mensaje
completo queda congelado en `pagos.invoice_email_outbox.payload_json`; un cambio
de plantilla, logo o configuración no cambia un reintento previo.
Esto se aplica a nuevos cobros; no hay un envío masivo ni reconstrucción histórica
automática de facturas emitidas antes de esta versión.

## Instalación pendiente de autorización por ambiente

1. Aplicar `migrations/20261125_invoice_delivery.sql` en la BD de pagos y
   `ATLAS-BACKEND/migrations/sql/20261125_checkout_email_delivery.sql` en la BD
   de usuarios. Son aditivas e idempotentes. No eliminan pagos ni reservas.
2. Backend: publicar el nuevo endpoint autenticado
   `/invoices/service/checkout_receipt`; requiere `SENDGRID_API_KEY`, `MAIL_FROM`
   y el `SERVICE_KEY` ya utilizado por `x-service-key`.
3. Pagos: instalar la nueva dependencia `reportlab`, incluir el logo en `public`,
   y configurar `ATLAS_API_BASE_URL`, `USER_SERVICE_KEY`, `PAYMENT_EMAIL_ENABLED=1`,
   `INVOICE_DELIVERY_SCOPE=qa` o `production` (estable y distinto por ambiente),
   `ACCOUNTING_INVOICE_EMAIL=dmonegro@enso.do`.
4. Desplegar pagos después de la migración y el backend. No se cambia AZUL_ENV,
   ningún certificado, precio, credencial del banco ni la activación de cobros.

El scope separa los workers de QA y producción aun si comparten una BD.
Para cualquier prueba que no sea una factura real autorizada, usar proveedores
falsos y direcciones de ejemplo; no usar a los clientes ni a la contable.

## Reintentos y resultados

El scheduler procesa la outbox cada 30 segundos. Hay exclusión por fila/lease,
ocho intentos máximos, y espera exponencial de 30 segundos hasta una hora.
Se reintenta únicamente el correo; jamás el cargo bancario.

La API de usuarios mantiene su propia clave durable en
`public.checkout_email_deliveries`. El mismo ID con otro contenido devuelve 409.
Una respuesta HTTP perdida entre pagos y usuarios puede repetirse sin otro
correo: se devuelve el resultado ya guardado antes de llamar a SendGrid.

- `ACCEPTED`: SendGrid devolvió 202; no significa que el destinatario lo leyó.
- `RETRYABLE`: fallo anterior al envío o rechazo explícito por límite (429).
- `SENDING`: otro worker tiene el envío en curso.
- `UNCERTAIN`: el proveedor pudo aceptar el correo pero se perdió su respuesta,
  o el proceso terminó sin poder guardar la confirmación. No se reenvía a ciegas.
- `FAILED`: rechazo definitivo/payload inválido o límite de intentos agotado.

Para un resultado incierto, conciliar con SendGrid usando el custom arg
`notification_id` y, si existe, `provider_id` (`X-Message-Id`). No borrar el ledger
ni cambiar la clave para forzar un envío. Reanudar solo tras confirmar que el
proveedor no lo aceptó. La interfaz nunca anuncia envío sin aceptación confirmada.
Recargar la página de resultado consulta la outbox; no vuelve a enviar el correo.

## Verificación local

- `python scripts/run_offline_tests.py` (bloquea red y excluye el gateway real).
- `python -m pytest tests/test_checkout_invoice_email.py` en ATLAS-BACKEND.
- `TEST_LOCAL_PG_PORT=<puerto local>` y `USER_SERVICE_SOURCE=<checkout backend>`:
  `python tests/check_postgresql_invoice_delivery.py`.
- `TEST_LOCAL_PG_PORT=<puerto local>`:
  `python tests/check_postgresql_payment_lifecycle.py`.
- `python scripts/preview_invoice.py --output <carpeta>` genera solo ejemplos.

Los checks PostgreSQL crean y eliminan exclusivamente una BD de prueba aleatoria
en loopback. La prueba integrada sustituye banco y SendGrid, y verifica migraciones,
factura persistida, adjuntos, ambos destinatarios, carreras en ambos servicios,
timeout, reintentos, caída de proceso, aislamiento y conservación de importes.
