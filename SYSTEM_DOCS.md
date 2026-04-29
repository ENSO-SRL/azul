# Atlas Pagos — Documentación del Sistema

> **Versión:** 0.4.0 | **Entorno:** Sandbox / Production | **Gateway:** AZUL Payment Gateway (República Dominicana)

---

## 1. Propósito y Objetivo

**Atlas Pagos** es una plataforma de procesamiento de pagos diseñada para negocios dominicanos que necesitan cobrar a sus clientes de forma recurrente, puntual, o mediante tarjetas guardadas — todo integrado con **AZUL**, el procesador de pagos líder en RD.

### Objetivos principales
- Procesar pagos únicos y recurrentes cumpliendo con las normativas **Visa/Mastercard** (indicadores CIT/MIT, stored credentials).
- Tokenizar tarjetas en **DataVault de AZUL** para evitar almacenar datos sensibles (PCI DSS).
- Automatizar el cobro mensual sin intervención humana, con reintentos inteligentes.
- Notificar al cliente en cada evento relevante de su suscripción.
- Detectar discrepancias entre los cobros locales y los reportes de AZUL (reconciliación).
- Soportar múltiples monedas (DOP y USD).

---

## 2. Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│                      CLIENTE / APP                      │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTPS
┌───────────────────────▼─────────────────────────────────┐
│               ATLAS API  (FastAPI / Python)              │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │   Routers    │ │   Services   │ │  Scheduler      │  │
│  │  (HTTP API)  │ │ (Biz logic)  │ │  (APScheduler)  │  │
│  └──────┬───────┘ └──────┬───────┘ └────────┬────────┘  │
│         └────────────────▼──────────────────┘           │
│  ┌──────────────────────────────────────────────────┐    │
│  │              Infrastructure Layer                │    │
│  │  AzulGateway (mTLS) │ SQLAlchemy ORM │ AWS SES  │    │
│  └──────────┬──────────┴───────┬────────┴──────────┘    │
└─────────────┼──────────────────┼────────────────────────┘
              │                  │
    ┌─────────▼──────┐  ┌────────▼────────┐
    │  AZUL API       │  │  PostgreSQL /   │
    │  (sandbox/prod) │  │  SQLite (local) │
    └────────────────┘  └─────────────────┘
```

### Stack tecnológico
| Capa | Tecnología |
|------|-----------|
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x async |
| Scheduler | APScheduler 3.x |
| HTTP client | httpx (mTLS) |
| Notificaciones | AWS SES (boto3) |
| Seguridad | mTLS + Auth headers + PAN masking |
| Base de datos | SQLite (dev) / PostgreSQL (prod) |
| Config | AWS Secrets Manager (prod) / .env (dev) |

---

## 3. Capas del Sistema (Clean Architecture)

```
app/

├── domain/
│   ├── entities.py          ← Modelos de negocio (Payment, RecurringPayment, ConsentRecord…)
│   └── repositories.py      ← Interfaces (puertos) de persistencia
├── services/
│   ├── payment_service.py   ← Pagos únicos, CIT, 3DS
│   ├── recurring_service.py ← Suscripciones, pause/resume/cancel, consent
│   ├── scheduler.py         ← Jobs automáticos (MIT, reminders, reconciliación)
│   ├── notification_service.py ← Emails vía AWS SES
│   └── reconciliation_service.py ← Cruce Atlas vs AZUL
└── infrastructure/
    ├── azul_gateway.py      ← Cliente HTTP mTLS hacia AZUL
    ├── azul_config.py       ← Config loader (env / AWS Secrets Manager)
    ├── models.py            ← ORM tables
    ├── repo_impl.py         ← Implementaciones concretas de repos
    └── database.py          ← Engine async SQLAlchemy

routers/
├── payments.py              ← Pagos únicos
├── recurring.py             ← Suscripciones recurrentes
├── tokens.py                ← DataVault (guardar/listar/eliminar tarjetas)
├── clubs.py                 ← Cobro on-demand con token
├── refunds.py               ← Void y Refund
├── threeds.py               ← Flujo 3DS 2.0
├── notifications.py         ← Test de notificaciones
└── reconciliation.py        ← Reconciliación bancaria
```

---

## 4. Base de Datos — Tablas

| Tabla | Propósito |
|-------|-----------|
| `payments` | Registro de cada intento de cobro |
| `recurring_payments` | Suscripciones activas/pausadas/canceladas |
| `consent_records` | Evidencia de consentimiento (Visa/MC) |
| `saved_cards` | Tarjetas tokenizadas por cliente |
| `transactions` | Log de cada llamada HTTP a AZUL |
| `reconciliation_reports` | Resultados del cruce diario contra AZUL |

---

## 5. Casos de Uso

### CU-01 — Pago único con tarjeta nueva
**Actor:** Cliente  
**Descripción:** El cliente ingresa su tarjeta para pagar un producto o servicio una sola vez.

**Historia de usuario:**
> Como cliente, quiero pagar con mi tarjeta de crédito sin crear una cuenta, para completar mi compra rápidamente.

**Flujo:**
1. Cliente envía número de tarjeta, expiración, CVC, monto.
2. Atlas envía `Sale` CIT a AZUL con `cardholderInitiatedIndicator: "1"`.
3. AZUL devuelve `IsoCode: "00"` (aprobado) o código de rechazo.
4. Atlas guarda el pago y retorna el resultado.

**Endpoint:** `POST /api/v1/payments`  
**Indicador:** CIT `"1"`  
**Variante:** `save_card: true` → tokeniza y devuelve `data_vault_token`.

---

### CU-02 — Suscripción mensual recurrente
**Actor:** Cliente (alta) + Sistema (cobros automáticos)  
**Descripción:** El cliente se suscribe y Atlas cobra automáticamente cada mes sin intervención del usuario.

**Historia de usuario:**
> Como negocio, quiero cobrar a mis clientes automáticamente cada mes, para no depender de que ellos recuerden pagar.

#### Fase 1 — Alta (CIT STANDING_ORDER)
1. Cliente ingresa tarjeta y acepta términos.
2. Atlas ejecuta `Sale` con `cardholderInitiatedIndicator: "STANDING_ORDER"` + `SaveToDataVault: "1"`.
3. AZUL cobra el primer mes y devuelve el token DataVault.
4. Atlas crea la suscripción con `next_charge_at = hoy + 30 días`.
5. Se registra el consentimiento del cliente (`POST /consent`).

#### Fase 2 — Cobros automáticos (MIT STANDING_ORDER)
El scheduler corre cada hora y:
1. Consulta suscripciones con `next_charge_at <= ahora` y `status = ACTIVE`.
2. Verifica que la tarjeta no esté vencida (`card_expiration YYYYMM`).
3. Ejecuta `Sale MIT` con `merchantInitiatedIndicator: "STANDING_ORDER"` + `ForceNo3DS: "1"`.
4. Si **aprobado**: avanza `next_charge_at += 30 días`, envía email de confirmación.
5. Si **declinado**: aplica política de reintentos, envía email de fallo.

#### Política de reintentos
| Intento | Espera |
|---------|--------|
| 1 | 1 día |
| 2 | 3 días |
| 3 | 7 días |
| 4+ | PAUSED |

**Endpoints:**
- `POST /api/v1/recurring` — crear suscripción
- `GET /api/v1/recurring?customer_id=` — listar
- `POST /api/v1/recurring/{id}/charge` — cobrar ahora (manual)
- `POST /api/v1/recurring/{id}/pause` — pausar
- `POST /api/v1/recurring/{id}/resume` — reanudar
- `DELETE /api/v1/recurring/{id}` — cancelar + DataVault DELETE
- `POST /api/v1/recurring/{id}/consent` — registrar consentimiento
- `GET /api/v1/recurring/{id}/history` — historial de cobros

---

### CU-03 — Cobro on-demand con tarjeta guardada (CIT)
**Actor:** Cliente  
**Descripción:** El cliente tiene una tarjeta guardada y paga sin reingresarla. Está presente en la sesión.

**Historia de usuario:**
> Como cliente de un club deportivo, quiero pagar mi cuota mensual con un clic, usando la tarjeta que ya registré.

**Flujo:**
1. Frontend envía `token` (DataVault) + monto.
2. Atlas ejecuta `Sale CIT` con `cardholderInitiatedIndicator: "STANDING_ORDER"`.
3. Resultado retornado inmediatamente.

**Endpoint:** `POST /api/v1/clubs/{club_id}/pay`  
**Indicador:** CIT `STANDING_ORDER`

---

### CU-04 — Pago de factura / servicio
**Actor:** Cliente  
**Descripción:** El cliente paga una factura específica (electricidad, agua, internet) referenciando su número de cuenta.

**Historia de usuario:**
> Como cliente, quiero pagar mi factura de electricidad ingresando el número de referencia, para tener constancia del pago.

**Flujo:**
1. Cliente envía tarjeta + `service_type` + `bill_reference`.
2. Atlas ejecuta `Sale` normal con campos de servicio.
3. Resultado persiste con la referencia de factura para trazabilidad.

**Endpoint:** `POST /api/v1/payments/service`

---

### CU-05 — Preautorización y captura (Hold/Post)
**Actor:** Negocio  
**Descripción:** Se bloquean fondos sin cobrar (reserva). El cobro efectivo ocurre al confirmar el servicio.

**Historia de usuario:**
> Como hotel, quiero bloquear el monto de la estadía al hacer check-in y cobrarlo al hacer check-out.

**Flujo:**
1. `POST /api/v1/payments/hold` → fondos bloqueados, no cobrados.
2. Al checkout: `POST /api/v1/payments/post` con el `AzulOrderId` del hold.
3. Si se cancela: no se hace Post, los fondos se liberan automáticamente.

---

### CU-06 — Tokenización sin cobrar
**Actor:** Cliente  
**Descripción:** El cliente registra su tarjeta durante el onboarding sin que se le cobre nada.

**Historia de usuario:**
> Como usuario nuevo, quiero registrar mi tarjeta al crear mi cuenta, para que los pagos futuros sean más rápidos.

**Endpoints:**
- `POST /api/v1/tokens` — registrar tarjeta
- `GET /api/v1/tokens/{customer_id}` — listar tarjetas del cliente
- `DELETE /api/v1/tokens/{token}` — eliminar tarjeta (DataVault DELETE)

---

### CU-07 — Anulación y devolución
**Actor:** Negocio / Soporte  
**Descripción:** Reversa de un cobro por error o insatisfacción del cliente.

| Caso | Endpoint | Condición |
|------|----------|-----------|
| Anulación sin costo | `POST /api/v1/refunds/void` | ≤ 20 min tras el cobro |
| Devolución con cargo | `POST /api/v1/refunds/refund` | > 20 min tras el cobro |

---

### CU-08 — Cobro MIT inmediato (fuera del ciclo)
**Actor:** Negocio (API)  
**Descripción:** El negocio quiere cobrar a un suscriptor ahora mismo sin esperar al scheduler.

**Casos de uso:**
- Cargo por sobreuso detectado hoy.
- Reactivar suscripción pausada y cobrar inmediatamente.
- Prueba manual en sandbox.

**Endpoint:** `POST /api/v1/recurring/{id}/charge`  
**Indicador:** MIT `STANDING_ORDER`

---

### CU-09 — Notificaciones por email
**Actor:** Sistema → Cliente  
**Descripción:** Atlas envía emails transaccionales automáticos en cada evento relevante.

**Historia de usuario:**
> Como cliente, quiero recibir un email cuando me cobren, cuando un cobro falle, o cuando mi suscripción sea pausada, para estar siempre informado.

| Evento | Disparador |
|--------|-----------|
| Cobro exitoso | Scheduler MIT aprobado |
| Cobro fallido | Scheduler MIT declinado |
| Suscripción pausada | 3 fallos consecutivos |
| Suscripción cancelada | Cliente cancela vía API |
| Tarjeta vencida | Scheduler detecta expiración |
| Aviso previo de cobro | 3 días antes (9:00am UTC) |

**Configuración:** `NOTIFY_FROM_EMAIL` en `.env`  
**Proveedor:** AWS SES  
**Fallback:** Log en consola si SES no está configurado  
**Test:** `POST /api/v1/notifications/test`

---

### CU-10 — Reconciliación bancaria
**Actor:** Sistema / Contabilidad  
**Descripción:** Cruza los pagos APPROVED en Atlas contra lo que AZUL reporta vía `verify_payment`. Detecta discrepancias antes de que lleguen al estado de cuenta.

**Historia de usuario:**
> Como contador, quiero que el sistema me avise si hay algún pago que Atlas marca como aprobado pero AZUL no reconoce, para hacer la conciliación bancaria correctamente.

**Flujo automático (00:30 UTC diario):**
1. Consulta todos los pagos `APPROVED` de las últimas 24h.
2. Llama `verify_payment` en AZUL por `CustomOrderId`.
3. Compara `IsoCode` local vs AZUL.
4. Persiste resultado en `reconciliation_reports`.
5. Si hay `MISMATCH` o `NOT_FOUND`, genera alerta en logs.

**Estados por fila:**
| Estado | Significado |
|--------|-------------|
| `OK` | Atlas y AZUL coinciden |
| `MISMATCH` | IsoCode diferente — revisar |
| `NOT_FOUND` | AZUL no encontró la transacción |
| `ERROR` | Error de red o integración |

**Endpoints:**
- `POST /api/v1/reconciliation/run` — ejecutar manualmente
- `GET /api/v1/reconciliation/report` — ver reporte completo
- `GET /api/v1/reconciliation/mismatches` — solo discrepancias

---

## 6. Autenticación y Seguridad

### mTLS (Mutual TLS)
Cada request a AZUL usa certificado cliente (`.crt` + `.key`). Configurado en `.env` o AWS Secrets Manager.

### Auth Headers
AZUL requiere dos headers de autenticación:
- `Auth1` / `Auth2` — diferentes valores para modo `splitit` vs `3dsecure`.

### PAN Masking
El número de tarjeta nunca se guarda en texto plano. El gateway enmascara los dígitos 7-15 antes de persistir cualquier log.

### DataVault
Las tarjetas se almacenan en los servidores de AZUL (no en Atlas). Atlas solo guarda el UUID del token. Al cancelar una suscripción, Atlas llama `TrxType=DELETE` para eliminar el token del vault (cumplimiento GDPR).

### Indicadores CIT/MIT (Visa/Mastercard mandatorio)
| Tipo | Indicador | Cuándo |
|------|-----------|--------|
| CIT genérico | `cardholderInitiatedIndicator: "1"` | Pago único |
| CIT recurrente | `cardholderInitiatedIndicator: "STANDING_ORDER"` | Primer cobro suscripción |
| MIT recurrente | `merchantInitiatedIndicator: "STANDING_ORDER"` | Scheduler / cobros automáticos |

### Consentimiento (Visa/MC mandatorio)
Para suscripciones, se debe registrar evidencia documentada de que el cliente autorizó los cobros futuros:
- Texto exacto mostrado al cliente
- IP del cliente al momento de aceptar
- Timestamp UTC de aceptación

Guardado en tabla `consent_records`. Endpoint: `POST /api/v1/recurring/{id}/consent`.

---

## 7. Idempotencia (Anti cobro duplicado)

Todos los endpoints de cobro aceptan el header `Idempotency-Key`. Si el mismo key se envía dos veces, el segundo request retorna el resultado del primero **sin llamar a AZUL nuevamente**.

```http
POST /api/v1/payments
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
```

El scheduler usa un `CustomOrderId` determinístico: `sha256(sub_id + attempt)[:20]` — mismo ID en reintento, evita cobros dobles si el job corre dos veces.

---

## 8. Multi-Currency

| Moneda | Campo API | CurrencyPosCode AZUL |
|--------|-----------|---------------------|
| Peso dominicano (DOP) | `"currency": "DOP"` | `"$"` |
| Dólar estadounidense (USD) | `"currency": "USD"` | `"US$"` |

Disponible en: `POST /payments`, `POST /clubs/{id}/pay`, `POST /recurring`.

---

## 9. Jobs del Scheduler

| Job | Frecuencia | Hora |
|-----|-----------|------|
| Cobro de suscripciones vencidas | Cada hora | — |
| Aviso de cobro próximo | Diario | 09:00 UTC |
| Reconciliación bancaria | Diario | 00:30 UTC |

---

## 10. Flujo 3DS 2.0

Para pagos de alto riesgo con `auth_mode: "3dsecure"`:

```
1. POST /api/v1/payments (con browser_info)
        │
        ▼
   PENDING_3DS_METHOD
        │  → renderizar threeds_method_form en iframe
        ▼
   POST /api/v1/3ds/method-notification
        │
        ▼
   PENDING_3DS_CHALLENGE (si banco requiere autenticación)
        │  → redirigir al banco (ACS)
        ▼
   POST /api/v1/3ds/term (banco redirige de vuelta)
        │
        ▼
   APPROVED / DECLINED
```

---

## 11. Endpoints — Referencia Completa

### Pagos únicos
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/payments` | Cobro CIT con tarjeta completa |
| `POST` | `/api/v1/payments/service` | Pago de factura/servicio |
| `POST` | `/api/v1/payments/hold` | Preautorización |
| `POST` | `/api/v1/payments/post` | Captura de preautorización |
| `POST` | `/api/v1/payments/verify` | Verificar transacción en AZUL |
| `GET` | `/api/v1/payments/{id}` | Consultar pago |

### Suscripciones
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/recurring` | Crear suscripción (CIT STANDING_ORDER) |
| `GET` | `/api/v1/recurring?customer_id=` | Listar por cliente |
| `GET` | `/api/v1/recurring/{id}` | Ver detalle |
| `POST` | `/api/v1/recurring/{id}/charge` | Cobrar ahora (MIT) |
| `POST` | `/api/v1/recurring/{id}/pause` | Pausar |
| `POST` | `/api/v1/recurring/{id}/resume` | Reanudar |
| `DELETE` | `/api/v1/recurring/{id}` | Cancelar + DataVault DELETE |
| `POST` | `/api/v1/recurring/{id}/consent` | Registrar consentimiento |
| `GET` | `/api/v1/recurring/{id}/consent` | Ver consentimiento |
| `GET` | `/api/v1/recurring/{id}/history` | Historial de cobros |

### Tokens / DataVault
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/tokens` | Tokenizar tarjeta |
| `GET` | `/api/v1/tokens/{customer_id}` | Listar tarjetas |
| `DELETE` | `/api/v1/tokens/{token}` | Eliminar tarjeta |

### Clubs (CIT on-demand)
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/clubs/{club_id}/pay` | Cobro con token guardado |

### Devoluciones
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/refunds/void` | Anular (≤ 20 min) |
| `POST` | `/api/v1/refunds/refund` | Devolver (> 20 min) |

### 3DS 2.0
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/3ds/method-notification` | Notificación del método 3DS |
| `POST` | `/api/v1/3ds/term` | Callback ACS tras challenge |

### Notificaciones
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/notifications/test` | Enviar notificación de prueba |
| `GET` | `/api/v1/notifications/status` | Ver config SES |

### Reconciliación
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/reconciliation/run` | Ejecutar manualmente |
| `GET` | `/api/v1/reconciliation/report` | Ver reporte |
| `GET` | `/api/v1/reconciliation/mismatches` | Ver discrepancias |

### Sistema
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/test/smoke` | Smoke test vs AZUL |
| `GET` | `/test/scheduler/run` | Disparar scheduler (sandbox) |
| `GET` | `/docs` | Swagger UI interactivo |

---

## 12. Variables de Entorno (.env)

```env
# === AZUL ===
AZUL_LOCAL_MODE=1              # 1=local, 0=AWS Secrets Manager
AZUL_MERCHANT_ID=...           # Merchant ID provisto por AZUL
AZUL_AUTH1=splitit
AZUL_AUTH2=splitit
AZUL_AUTH1_3DS=3dsecure
AZUL_AUTH2_3DS=3dsecure
AZUL_CERT_PATH=/ruta/al.crt
AZUL_KEY_PATH=/ruta/al.key
AZUL_ENV=sandbox               # sandbox | production

# === Notificaciones ===
NOTIFY_FROM_EMAIL=             # Email verificado en AWS SES
NOTIFY_AWS_REGION=us-east-1
NOTIFY_ENABLED=1               # 0 = solo logs

# === App ===
APP_BASE_URL=https://tu-dominio.com
DEFAULT_CURRENCY=DOP
```

---

## 13. Configuración de Producción

### AWS Secrets Manager
En producción (`AZUL_LOCAL_MODE=0`), las credenciales se leen de tres secrets:

| Secret | Contenido |
|--------|-----------|
| `iamatlas/azul/dev/api-credentials` | JSON con merchant_id, auth_splitit, auth_3dsecure, env |
| `iamatlas/azul/dev/cert-pem` | Certificado PEM (cuerpo) |
| `iamatlas/azul/dev/cert-key` | Clave privada PEM |

### Inicio del servidor
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Swagger UI
```
http://localhost:8000/docs
```

---

## 14. Códigos de Respuesta AZUL

| IsoCode | Significado |
|---------|-------------|
| `00` | Aprobado |
| `01` | Referir al banco |
| `05` | No autorizado |
| `12` | Transacción inválida |
| `14` | Número de tarjeta inválido |
| `51` | Fondos insuficientes |
| `54` | Tarjeta vencida |
| `57` | Transacción no permitida |
| `65` | Límite excedido |
| `91` | Banco no disponible |
| `Error` | Error de integración (ver logs) |

---

## 15. Ciclo de Vida de una Suscripción

```
                    POST /recurring
                          │
                          ▼
                       ACTIVE ◄──────── POST /resume
                          │                   ▲
              ┌───────────┴───────────┐       │
              │                       │       │
         Cobro exitoso         3 fallos / vencida
              │                       │       │
              ▼                       ▼       │
    next_charge_at += 30d         PAUSED ─────┘
              │
              │  DELETE /recurring/{id}
              ▼
          CANCELLED
      (DataVault DELETE)
```

---

*Documentación generada para Atlas Pagos v0.4.0 — 2026*
