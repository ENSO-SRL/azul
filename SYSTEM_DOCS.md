# Atlas Pagos — Documentación del Sistema

> **Versión:** 0.5.0 | **Entorno:** Sandbox / Production | **Gateway:** AZUL Payment Gateway (República Dominicana)

---

## 1. Propósito y Objetivo

**Atlas Pagos** es una plataforma de procesamiento de pagos diseñada para negocios dominicanos que necesitan cobrar a sus clientes de forma recurrente, puntual, o mediante tarjetas guardadas — todo integrado con **AZUL**, el procesador de pagos líder en RD.

### Objetivos principales
- Procesar pagos únicos y recurrentes cumpliendo con las normativas **Visa/Mastercard** (indicadores CIT/MIT, stored credentials).
- Tokenizar tarjetas en **DataVault de AZUL** para evitar almacenar datos sensibles (PCI DSS).
- Automatizar el cobro mensual sin intervención humana, con reintentos inteligentes.
- Ofrecer **7 días de gracia** a usuarios nuevos antes de iniciar cobros automáticos.
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
    │  (sandbox/prod) │  │  PostgreSQL RDS  │
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
| Base de datos | PostgreSQL (AWS RDS — `Atlas_User_Service`) |
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
│   ├── recurring_service.py ← Suscripciones, pause/resume/cancel, consent, estado cliente
│   ├── post_payment.py      ← Acciones post-pago (email, guardar tarjeta, crear suscripción con trial)
│   ├── token_service.py     ← Gestión de tarjetas DataVault
│   ├── scheduler.py         ← Jobs automáticos (MIT, reminders, reconciliación)
│   ├── notification_service.py ← Emails vía AWS SES
│   └── reconciliation_service.py ← Cruce Atlas vs AZUL
└── infrastructure/
    ├── azul_gateway.py      ← Cliente HTTP mTLS hacia AZUL
    ├── azul_config.py       ← Config loader (env / AWS Secrets Manager)
    ├── models.py            ← ORM tables (schema pagos)
    ├── repo_impl.py         ← Implementaciones concretas de repos
    ├── repo_saved_cards.py  ← Repositorio de tarjetas guardadas
    └── database.py          ← Engine async SQLAlchemy

routers/
├── payments.py              ← Pagos únicos
├── recurring.py             ← Suscripciones recurrentes
├── tokens.py                ← DataVault (guardar/listar/eliminar tarjetas) + estado del usuario
├── checkout.py              ← Formulario HTML de pago + flujo 3DS + suscripción automática
├── clubs.py                 ← Cobro on-demand con token
├── refunds.py               ← Void y Refund
├── threeds.py               ← Flujo 3DS 2.0
├── notifications.py         ← Test de notificaciones
└── reconciliation.py        ← Reconciliación bancaria
```

---

## 4. Base de Datos — Tablas

Todas las tablas viven en el schema **`pagos`**.

### `pagos.payments` — Cada intento de cobro
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID del pago |
| `order_id` | `String(100)` | ID de orden (ej. `CHK-A1B2C3D4`, `sub-{id}`) |
| `amount` | `Integer` | Monto en centavos (ej. 5000 = RD$50.00) |
| `itbis` | `Integer` | ITBIS en centavos |
| `currency` | `String(5)` | CurrencyPosCode (`$` = DOP, `US$` = USD) |
| `card_number_masked` | `String(20)` | Últimos 4 dígitos visibles |
| `payment_type` | `String(20)` | `SALE`, `SERVICE`, `RECURRING`, `CLUB` |
| `status` | `String(20)` | `PENDING`, `APPROVED`, `DECLINED`, `ERROR`, `VOIDED`, `REFUNDED`, `PENDING_3DS_METHOD`, `PENDING_3DS_CHALLENGE` |
| `auth_mode` | `String(20)` | `splitit` o `3dsecure` |
| `initiated_by` | `String(20)` | `cardholder` (CIT) o `merchant` (MIT) |
| `customer_id` | `String(100)` | UUID del usuario Atlas |
| `idempotency_key` | `String(128)` | Evita cobros duplicados (nullable) |
| `azul_order_id` | `String(50)` | Order ID de Azul |
| `iso_code` | `String(10)` | `00` = aprobado |
| `response_code` | `String(20)` | `ISO8583` o `Error` |
| `response_message` | `String(255)` | Mensaje legible |
| `authorization_code` | `String(20)` | Código de autorización (disputas) |
| `rrn` | `String(30)` | Referencia del adquirente |
| `data_vault_token` | `String(100)` | Token DataVault (si se tokenizó) |
| `cardholder_name` | `String(100)` | Nombre del titular |
| `cardholder_email` | `String(255)` | Email del titular |
| `service_type` | `String(50)` | Tipo de servicio (solo `SERVICE`) |
| `bill_reference` | `String(100)` | Referencia de factura |
| `threeds_*` | varios | Campos 3DS 2.0 (method_form, challenge_form, redirect_url) |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | Timestamps |

### `pagos.saved_cards` — Tarjetas tokenizadas
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID |
| `customer_id` | `String(100)` | Dueño de la tarjeta |
| `token` | `String(100)` UNIQUE | Token DataVault de AZUL |
| `card_brand` | `String(20)` | `VISA`, `MASTERCARD`, etc. |
| `card_last4` | `String(4)` | Últimos 4 dígitos |
| `expiration` | `String(6)` | `YYYYMM` |
| `is_default` | `Boolean` | Tarjeta predeterminada |
| `created_at` | `TIMESTAMPTZ` | — |

### `pagos.recurring_payments` — Suscripciones
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID de la suscripción |
| `customer_id` | `String(100)` | UUID del usuario Atlas |
| `amount` | `Integer` | Monto recurrente en centavos |
| `itbis` | `Integer` | ITBIS en centavos |
| `frequency_days` | `Integer` | Frecuencia de cobro (default: 30) |
| `description` | `String(255)` | Ej. `"Membresía Atlas"` |
| `status` | `String(20)` | **`ACTIVE`** / **`PAUSED`** / **`CANCELLED`** |
| `data_vault_token` | `String(100)` | Token para cobros MIT |
| `card_brand` | `String(20)` | Marca de la tarjeta |
| `card_last4` | `String(4)` | Últimos 4 dígitos |
| `card_expiration` | `String(6)` | `YYYYMM` |
| `cardholder_email` | `String(255)` | Para notificaciones |
| `next_charge_at` | `TIMESTAMPTZ` | Próximo cobro programado |
| `last_charged_at` | `TIMESTAMPTZ` | Último cobro exitoso |
| `failed_attempts` | `Integer` | Intentos fallidos consecutivos |
| `last_failure_reason` | `String(500)` | Razón del último fallo |
| `trial_ends_at` | `TIMESTAMPTZ` | **Fin del período de gracia** (`NULL` = sin trial) |
| `created_at` | `TIMESTAMPTZ` | — |

### `pagos.consent_records` — Consentimiento (Visa/MC)
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID |
| `subscription_id` | `String(36)` | FK a `recurring_payments.id` |
| `customer_id` | `String(100)` | UUID del usuario |
| `consent_text` | `Text` | Texto exacto mostrado al cliente |
| `ip_address` | `String(45)` | IP del cliente |
| `user_agent` | `String(500)` | Navegador del cliente |
| `consented_at` | `TIMESTAMPTZ` | Fecha/hora de aceptación |

### `pagos.transactions` — Log de llamadas a AZUL
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID |
| `payment_id` | `String(36)` | FK a `payments.id` |
| `request_payload` | `Text` | JSON enviado (PAN enmascarado) |
| `response_payload` | `Text` | JSON recibido de AZUL |
| `http_status` | `Integer` | HTTP status |
| `iso_code` | `String(10)` | `00` = OK |
| `response_message` | `String(255)` | Mensaje legible |
| `created_at` | `TIMESTAMPTZ` | — |

### `pagos.reconciliation_reports` — Reconciliación diaria
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | `String(36)` PK | UUID |
| `run_date` | `String(10)` | `YYYY-MM-DD` |
| `payment_id` | `String(36)` | FK a `payments.id` |
| `local_status` / `azul_status` | `String(20)` | Status local vs AZUL |
| `status` | `String(20)` | `OK`, `MISMATCH`, `NOT_FOUND`, `ERROR` |
| `notes` | `String(500)` | Observaciones |
| `checked_at` | `TIMESTAMPTZ` | — |

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

### CU-02 — Checkout con suscripción automática y período de gracia
**Actor:** Cliente (alta vía checkout) + Sistema (cobros automáticos)
**Descripción:** El cliente ingresa su tarjeta en el formulario de checkout. Si es un usuario **nuevo**, recibe 7 días de gracia antes del primer cobro recurrente. Si es **existente**, el cobro inicia de inmediato.

**Historia de usuario:**
> Como usuario nuevo de Atlas, quiero registrar mi tarjeta y tener unos días para probar el servicio antes de que me cobren.

#### Flujo — Checkout con trial (usuario nuevo)

```
  GET /checkout
       │  (formulario HTML con 3DS, tarjetas guardadas, validación Luhn)
       ▼
  POST /checkout/process
       │
       ├── 3DS Method? → iframe silencioso → POST /checkout/3ds-continue
       ├── 3DS Challenge? → GET /checkout/challenge/{id} → redirect ACS
       │
       ▼
  AZUL responde APPROVED (RD$2.36 validación + tokenización)
       │
       ├── handle_post_payment_actions()  ← email de recibo, confirmación
       │
       └── create_subscription_if_needed()
            │
            ├── ¿Usuario nuevo? (sin tarjetas ni suscripciones previas)
            │     → Crear suscripción ACTIVE
            │     → trial_ends_at = hoy + 7 días
            │     → next_charge_at = hoy + 7 días
            │     → last_charged_at = NULL
            │
            └── ¿Usuario existente?
                  → Crear suscripción ACTIVE
                  → trial_ends_at = NULL
                  → next_charge_at = hoy + 30 días
                  → last_charged_at = hoy
```

#### Detección de usuario nuevo
El sistema verifica en la base de datos:
1. `pagos.recurring_payments` — ¿tiene suscripciones previas (cualquier status)?
2. `pagos.saved_cards` — ¿tiene tarjetas guardadas?

Si **ambas** están vacías → **usuario nuevo** → trial de 7 días.

#### Tabla resumen
| Tipo de usuario | Validación RD$2.36 | Suscripción | `next_charge_at` | `trial_ends_at` |
|---|---|---|---|---|
| **Nuevo** | ✅ | ACTIVE | `hoy + 7 días` | `hoy + 7 días` |
| **Existente** | ✅ | ACTIVE | `hoy + 30 días` | `NULL` |

#### Protección anti-duplicados
Si el usuario ya tiene una suscripción `ACTIVE`, no se crea otra.

**Endpoints del checkout:**
- `GET /checkout` — Formulario de pago (lee cookie JWT, carga tarjetas guardadas)
- `POST /checkout/process` — Procesar pago
- `POST /checkout/3ds-continue` — Continuar tras 3DS Method
- `GET /checkout/challenge/{payment_id}` — Página del challenge 3DS
- `GET /checkout/result/{payment_id}` — Resultado final (tras 3DS)

---

### CU-03 — Suscripción mensual recurrente (vía API)
**Actor:** Cliente (alta) + Sistema (cobros automáticos)
**Descripción:** El cliente se suscribe vía API y Atlas cobra automáticamente cada mes sin intervención del usuario.

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
- `GET /api/v1/recurring/customer-status?customer_id=` — estado (incluye `in_trial`)
- `POST /api/v1/recurring/{id}/charge` — cobrar ahora (manual)
- `POST /api/v1/recurring/{id}/pause` — pausar
- `POST /api/v1/recurring/{id}/resume` — reanudar
- `DELETE /api/v1/recurring/{id}` — cancelar + DataVault DELETE
- `POST /api/v1/recurring/{id}/consent` — registrar consentimiento
- `GET /api/v1/recurring/{id}/consent` — ver consentimiento
- `GET /api/v1/recurring/{id}/history` — historial de cobros

---

### CU-04 — Cobro on-demand con tarjeta guardada (CIT)
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

### CU-05 — Pago de factura / servicio
**Actor:** Cliente
**Descripción:** El cliente paga una factura específica (electricidad, agua, internet) referenciando su número de cuenta.

**Flujo:**
1. Cliente envía tarjeta + `service_type` + `bill_reference`.
2. Atlas ejecuta `Sale` normal con campos de servicio.
3. Resultado persiste con la referencia de factura para trazabilidad.

**Endpoint:** `POST /api/v1/payments/service`

---

### CU-06 — Preautorización y captura (Hold/Post)
**Actor:** Negocio
**Descripción:** Se bloquean fondos sin cobrar (reserva). El cobro efectivo ocurre al confirmar el servicio.

**Flujo:**
1. `POST /api/v1/payments/hold` → fondos bloqueados, no cobrados.
2. Al checkout: `POST /api/v1/payments/post` con el `AzulOrderId` del hold.
3. Si se cancela: no se hace Post, los fondos se liberan automáticamente.

---

### CU-07 — Tokenización sin cobrar
**Actor:** Cliente
**Descripción:** El cliente registra su tarjeta durante el onboarding sin que se le cobre nada.

**Endpoints:**
- `POST /api/v1/tokens` — registrar tarjeta (DataVault CREATE)
- `GET /api/v1/tokens/{customer_id}` — listar tarjetas del cliente
- `GET /api/v1/tokens/by-email/{email}` — listar tarjetas por email (datos seguros, token enmascarado)
- `GET /api/v1/tokens/status/{customer_id}` — estado integral del usuario (tarjetas, suscripciones, pagos, trial)
- `PUT /api/v1/tokens/{customer_id}/default/{card_id}` — marcar como predeterminada
- `DELETE /api/v1/tokens/{token}` — eliminar tarjeta (DataVault DELETE)
- `DELETE /api/v1/tokens/by-email/{email}/{card_id}` — eliminar tarjeta por ID y email

---

### CU-08 — Anulación y devolución
**Actor:** Negocio / Soporte

| Caso | Endpoint | Condición |
|------|----------|-----------|
| Anulación sin costo | `POST /api/v1/refunds/void` | ≤ 20 min tras el cobro |
| Devolución con cargo | `POST /api/v1/refunds/refund` | > 20 min tras el cobro |

---

### CU-09 — Cobro MIT inmediato (fuera del ciclo)
**Actor:** Negocio (API)
**Descripción:** El negocio quiere cobrar a un suscriptor ahora mismo sin esperar al scheduler.

**Endpoint:** `POST /api/v1/recurring/{id}/charge`
**Indicador:** MIT `STANDING_ORDER`

---

### CU-10 — Notificaciones por email
**Actor:** Sistema → Cliente

| Evento | Disparador |
|--------|-----------|
| Cobro exitoso | Scheduler MIT aprobado |
| Cobro fallido | Scheduler MIT declinado |
| Suscripción pausada | 3 fallos consecutivos |
| Suscripción cancelada | Cliente cancela vía API |
| Tarjeta vencida | Scheduler detecta expiración |
| Aviso previo de cobro | 3 días antes (9:00am UTC) |
| Recibo de checkout | Pago aprobado vía checkout |

**Configuración:** `NOTIFY_FROM_EMAIL` en `.env`
**Proveedor:** AWS SES
**Fallback:** Log en consola si SES no está configurado
**Test:** `POST /api/v1/notifications/test`

---

### CU-11 — Reconciliación bancaria
**Actor:** Sistema / Contabilidad
**Descripción:** Cruza los pagos APPROVED en Atlas contra lo que AZUL reporta vía `verify_payment`.

**Flujo automático (00:30 UTC diario):**
1. Consulta todos los pagos `APPROVED` de las últimas 24h.
2. Llama `verify_payment` en AZUL por `CustomOrderId`.
3. Compara `IsoCode` local vs AZUL.
4. Persiste resultado en `reconciliation_reports`.
5. Si hay `MISMATCH` o `NOT_FOUND`, genera alerta en logs.

**Endpoints:**
- `POST /api/v1/reconciliation/run` — ejecutar manualmente
- `GET /api/v1/reconciliation/report` — ver reporte completo
- `GET /api/v1/reconciliation/mismatches` — solo discrepancias

---

## 6. Período de Gracia (Trial) — 7 días

### Regla de negocio
Los usuarios que **nunca han registrado una tarjeta ni tenido una suscripción** reciben 7 días de gracia cuando pasan por el checkout. Durante ese período tienen acceso completo al servicio sin cobros recurrentes.

### Cómo se determina
| Condición | ¿Es nuevo? |
|-----------|-----------|
| Sin filas en `pagos.recurring_payments` AND sin filas en `pagos.saved_cards` | ✅ Nuevo → trial 7 días |
| Tiene al menos una fila en cualquiera de esas tablas | ❌ Existente → cobro inmediato |

### Campo en base de datos
`pagos.recurring_payments.trial_ends_at` — `TIMESTAMPTZ` nullable:
- **Con valor futuro** → el usuario está en período de gracia
- **`NULL`** → no tiene trial (usuario existente o trial ya venció)

### APIs que reportan el trial

**`GET /api/v1/recurring/customer-status?customer_id=...`**
```json
{
  "customer_id": "usr_123",
  "is_active": true,
  "is_current": true,
  "in_trial": true,
  "trial_ends_at": "2026-07-27T16:30:00+00:00",
  "subscriptions": [
    {
      "subscription_id": "abc-123",
      "status": "ACTIVE",
      "in_trial": true,
      "trial_ends_at": "2026-07-27T16:30:00+00:00",
      "next_charge_at": "2026-07-27T16:30:00+00:00",
      "last_charged_at": null
    }
  ]
}
```

**`GET /api/v1/tokens/status/{customer_id}`**
```json
{
  "summary": {
    "status": "trial",
    "message": "Período de gracia activo. Primer cobro programado para 2026-07-27T16:30:00+00:00.",
    "in_trial": true,
    "trial_ends_at": "2026-07-27T16:30:00+00:00"
  }
}
```

### Posibles valores de `summary.status`
| Valor | Significado |
|-------|-------------|
| `trial` | Período de gracia activo (usuario nuevo) |
| `active` | Suscripción al día, sin problemas |
| `payment_issues` | Suscripción pausada por fallos de cobro |
| `retrying` | Reintentos pendientes |
| `card_expired` | Todas las tarjetas vencidas |
| `no_card` | Sin tarjetas guardadas |
| `no_subscription` | Tiene tarjeta pero no suscripción |

---

## 7. Autenticación y Seguridad

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

### Checkout — JWT y pre-llenado
El checkout lee la cookie `access_token` (JWT del auth service) para:
- Identificar al usuario (`sub` claim)
- Pre-llenar nombre y email
- Cargar tarjetas guardadas automáticamente

---

## 8. Idempotencia (Anti cobro duplicado)

Todos los endpoints de cobro aceptan el header `Idempotency-Key`. Si el mismo key se envía dos veces, el segundo request retorna el resultado del primero **sin llamar a AZUL nuevamente**.

```http
POST /api/v1/payments
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
```

El scheduler usa un `CustomOrderId` determinístico: `sub-{id[:12]}-c{YYYYMMDD}-att{intento}`
(derivado del ID de suscripción, la **fecha de cobro completa** y el número de intento).
La fecha completa —no el mes— evita colisiones cuando dos cobros de 30 días caen en el
mismo mes calendario (ej. 1 y 31 de enero). Además, antes de cobrar el scheduler consulta
si ya existe un pago APROBADO con ese ID (idempotency latch): si el proceso murió después
de cobrar pero antes de avanzar `next_charge_at`, la siguiente corrida **avanza el ciclo
sin volver a cobrar**, evitando el doble cobro.

---

## 9. Multi-Currency

| Moneda | Campo API | CurrencyPosCode AZUL |
|--------|-----------|---------------------|
| Peso dominicano (DOP) | `"currency": "DOP"` | `"$"` |
| Dólar estadounidense (USD) | `"currency": "USD"` | `"US$"` |

Disponible en: `POST /payments`, `POST /clubs/{id}/pay`, `POST /recurring`.

---

## 10. Jobs del Scheduler

| Job | Frecuencia | Hora |
|-----|-----------|------|
| Cobro de suscripciones vencidas | Cada hora | — |
| Aviso de cobro próximo | Diario | 09:00 UTC |
| Reconciliación bancaria | Diario | 00:30 UTC |

---

## 11. Flujo 3DS 2.0

Para pagos de alto riesgo con `auth_mode: "3dsecure"`:

```
1. POST /checkout/process (o POST /api/v1/payments con browser_info)
        │
        ▼
   PENDING_3DS_METHOD
        │  → renderizar threeds_method_form en iframe silencioso
        ▼
   POST /checkout/3ds-continue (o POST /api/v1/3ds/method-notification)
        │
        ▼
   PENDING_3DS_CHALLENGE (si banco requiere autenticación)
        │  → GET /checkout/challenge/{id} → redirect al ACS del banco
        ▼
   POST /api/v1/3ds/term (banco redirige de vuelta)
        │
        ▼
   APPROVED / DECLINED
        │
        └── Si APPROVED: post-payment actions + crear suscripción
```

---

## 12. Endpoints — Referencia Completa

### Pagos únicos
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/payments` | Cobro CIT con tarjeta completa |
| `POST` | `/api/v1/payments/service` | Pago de factura/servicio |
| `POST` | `/api/v1/payments/hold` | Preautorización |
| `POST` | `/api/v1/payments/post` | Captura de preautorización |
| `POST` | `/api/v1/payments/verify` | Verificar transacción en AZUL |
| `GET` | `/api/v1/payments/{id}` | Consultar pago |

### Checkout (UI)
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/checkout` | Formulario de pago HTML (lee JWT, carga tarjetas) |
| `POST` | `/checkout/process` | Procesar pago → 3DS → suscripción automática |
| `POST` | `/checkout/3ds-continue` | Continuar tras 3DS Method |
| `GET` | `/checkout/challenge/{id}` | Renderizar challenge 3DS |
| `GET` | `/checkout/result/{id}` | Resultado final post-3DS |

### Suscripciones
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/recurring` | Crear suscripción (CIT STANDING_ORDER) |
| `GET` | `/api/v1/recurring?customer_id=` | Listar por cliente |
| `GET` | `/api/v1/recurring/customer-status?customer_id=` | Estado de pago (**incluye `in_trial`**) |
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
| `POST` | `/api/v1/tokens` | Tokenizar tarjeta (sin cobrar) |
| `GET` | `/api/v1/tokens/status/{customer_id}` | Estado integral (**incluye `in_trial`**) |
| `GET` | `/api/v1/tokens/by-email/{email}` | Tarjetas por email (datos seguros) |
| `GET` | `/api/v1/tokens/{customer_id}` | Listar tarjetas (datos completos) |
| `PUT` | `/api/v1/tokens/{customer_id}/default/{card_id}` | Marcar predeterminada |
| `DELETE` | `/api/v1/tokens/{token}` | Eliminar tarjeta |
| `DELETE` | `/api/v1/tokens/by-email/{email}/{card_id}` | Eliminar por ID y email |

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

## 13. Variables de Entorno (.env)

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

# === Base de datos ===
DATABASE_URL=postgresql+asyncpg://...
DB_SSL=require                 # disable para desarrollo local
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10

# === Auth Service ===
AUTH_API_BASE_URL=https://api.iamatlas.do

# === Notificaciones ===
NOTIFY_FROM_EMAIL=             # Email verificado en AWS SES
NOTIFY_AWS_REGION=us-east-1
NOTIFY_ENABLED=1               # 0 = solo logs

# === App ===
APP_BASE_URL=https://tu-dominio.com
DEFAULT_CURRENCY=DOP
```

---

## 14. Configuración de Producción

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

## 15. Códigos de Respuesta AZUL

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
| `3D` | Challenge 3DS requerido |
| `3D2METHOD` | 3DS Method requerido |
| `Error` | Error de integración (ver logs) |

---

## 16. Ciclo de Vida de una Suscripción

```
                    POST /checkout/process
                    (o POST /api/v1/recurring)
                          │
              ┌───────────┴───────────┐
              │                       │
         Usuario NUEVO          Usuario EXISTENTE
              │                       │
              ▼                       ▼
     ACTIVE (trial 7d)         ACTIVE (sin trial)
     next_charge_at             next_charge_at
     = hoy + 7 días             = hoy + 30 días
              │                       │
              └───────────┬───────────┘
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

## 17. Acciones Post-Pago (`post_payment.py`)

Cuando un checkout es APPROVED, se ejecutan las siguientes acciones:

| # | Acción | Descripción |
|---|--------|-------------|
| 1 | Verificar tarjeta guardada | Detecta si `data_vault_token` está presente |
| 2 | Enviar recibo | Email vía SES con monto, tarjeta, referencia |
| 3 | Trigger confirmación auth | Llama al auth service para confirmar usuario |
| 4 | Crear suscripción | `create_subscription_if_needed()` — trial para nuevos |

Estas acciones **nunca bloquean** el response del checkout. Los errores se loguean pero no se propagan.

---

*Documentación generada para Atlas Pagos v0.5.0 — Julio 2026*
