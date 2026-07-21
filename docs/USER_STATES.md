# Atlas Pagos — Estados del Usuario / Cliente

> **Versión:** 0.5.0 | **Fecha:** Julio 2026

---

## 1. Resumen

Un "usuario" (o `customer`) en Atlas Pagos se identifica por un `customer_id` único. Su **estado real** se determina combinando tres dimensiones:

| Dimensión | ¿Dónde vive? | Posibles valores |
|-----------|-------------|-----------------|
| **Estado de suscripción** | `recurring_payments.status` | `ACTIVE`, `PAUSED`, `CANCELLED` |
| **Estado de pago** | `payments.status` | `PENDING`, `PENDING_3DS_METHOD`, `PENDING_3DS_CHALLENGE`, `APPROVED`, `DECLINED`, `ERROR`, `VOIDED`, `REFUNDED` |
| **Estado consolidado** | Calculado en runtime | `is_active`, `is_current`, `has_overdue_payment`, `in_trial` |

No existe un campo único "user_status" — el estado del cliente se **compone** de sus suscripciones y pagos.

---

## 2. Estados de Suscripción (`SubscriptionStatus`)

> Definido en [`entities.py`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/domain/entities.py#L68-L71)

| Estado | Valor | Significado |
|--------|-------|-------------|
| 🟢 **ACTIVE** | `"ACTIVE"` | Suscripción vigente. El scheduler intenta cobros automáticos cada ciclo (30 días por defecto). |
| 🟠 **PAUSED** | `"PAUSED"` | Suscripción suspendida. No se realizan cobros. Puede ser reactivada. La tarjeta permanece en DataVault. |
| 🔴 **CANCELLED** | `"CANCELLED"` | Suscripción cancelada definitivamente. El token DataVault se elimina (DELETE). No se puede reactivar. |

### Diagrama de transiciones

```
                    POST /api/v1/recurring
                    (primer cobro CIT exitoso)
                           │
                           ▼
    ┌─────────────────── ACTIVE ◄──────────────────────┐
    │                      │                            │
    │         ┌────────────┼──────────────┐             │
    │         │            │              │             │
    │    Cobro exitoso   3+ fallos    Tarjeta       POST /{id}/resume
    │    (scheduler)    consecutivos   vencida     (reset failed_attempts)
    │         │            │              │             │
    │         ▼            ▼              ▼             │
    │   next_charge_at   PAUSED ─────────┘             │
    │    += 30 días       ⛔ Auto-pause                │
    │         │                                        │
    │         │                                        │
    │   DELETE /{id}                                   │
    │         │                                        │
    │         ▼                                        │
    │     CANCELLED                                    │
    │  (DataVault DELETE)                              │
    │  ❌ Irreversible                                  │
    └──────────────────────────────────────────────────┘
```

### ¿Cómo llega un usuario a cada estado?

#### → ACTIVE
- **Checkout exitoso** → `create_subscription_if_needed()` crea la suscripción con status ACTIVE
- **POST /api/v1/recurring** → `create_subscription()` crea vía API directa
- **POST /{id}/resume** → reactivar una suscripción PAUSED

#### → PAUSED (automático)
- **4 fallos consecutivos** → El scheduler pausa automáticamente después de agotar reintentos
- **Tarjeta vencida** → El scheduler detecta que `card_expiration` ya pasó y pausa sin intentar cobrar
- **Manual** → `POST /{id}/pause` desde el API

#### → CANCELLED (irreversible)
- **DELETE /{id}** o **POST /{id}/cancel** → Cancela la suscripción
- El token DataVault se elimina de los servidores de AZUL (best-effort)
- Se envía email de notificación al cliente

---

## 3. Estados de Pago (`PaymentStatus`)

> Definido en [`entities.py`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/domain/entities.py#L50-L58)

| Estado | Valor | Significado |
|--------|-------|-------------|
| ⏳ **PENDING** | `"PENDING"` | Pago creado, aún no enviado a AZUL |
| 🔐 **PENDING_3DS_METHOD** | `"PENDING_3DS_METHOD"` | AZUL respondió `3D2METHOD` — esperando que el iframe 3DS Method ejecute |
| 🔐 **PENDING_3DS_CHALLENGE** | `"PENDING_3DS_CHALLENGE"` | AZUL respondió `3D` — el usuario debe completar el challenge en el ACS del emisor |
| ✅ **APPROVED** | `"APPROVED"` | Pago aprobado por AZUL (IsoCode `"00"`) |
| ❌ **DECLINED** | `"DECLINED"` | Pago rechazado (fondos insuficientes, tarjeta inválida, etc.) |
| ⚠️ **ERROR** | `"ERROR"` | Error de integración (credenciales inválidas, timeout, error AZUL) |
| 🔄 **VOIDED** | `"VOIDED"` | Pago anulado (void — mismo día) |
| 💰 **REFUNDED** | `"REFUNDED"` | Pago reembolsado (refund — después del día de la transacción) |

### Diagrama de flujo de pago

```
         Nuevo pago
              │
              ▼
           PENDING
              │
     ┌────────┼──────────────┐
     │        │              │
  IsoCode   IsoCode       IsoCode
   "00"    "3D2METHOD"     "3D"
     │        │              │
     ▼        ▼              ▼
  APPROVED  PENDING_      PENDING_
     │      3DS_METHOD    3DS_CHALLENGE
     │        │              │
     │     iframe OK     challenge OK
     │        │              │
     │     ┌──┴──────────────┘
     │     ▼
     │  Re-procesado por AZUL
     │     │
     │  ┌──┼────────────┐
     │  │  │            │
     │  │ "00"       otro
     │  │  │            │
     │  ▼  ▼            ▼
     │ APPROVED     DECLINED/ERROR
     │
     ├───── POST /void ────────► VOIDED
     │
     └───── POST /refund ──────► REFUNDED
```

---

## 4. Estado Consolidado del Cliente

> Calculado en [`recurring_service.py → get_customer_status()`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/recurring_service.py#L330-L431)  
> Endpoint: `GET /api/v1/recurring/customer-status?customer_id={id}`

El sistema evalúa **todas** las suscripciones del cliente y retorna un estado consolidado:

### Campos de estado

| Campo | Tipo | Lógica |
|-------|------|--------|
| `is_active` | `bool` | `true` si tiene **al menos una** suscripción `ACTIVE` |
| `is_current` | `bool` | `true` si `is_active` **Y** ningún pago está vencido ni fallido |
| `has_overdue_payment` | `bool` | `true` si alguna suscripción ACTIVE tiene `failed_attempts > 0` o `next_charge_at < now` |
| `in_trial` | `bool` | `true` si alguna suscripción ACTIVE tiene `trial_ends_at > now` (período de gracia de 7 días) |

### Tabla de decisión — ¿Qué mostrar al usuario?

| `is_active` | `is_current` | `has_overdue` | `in_trial` | Situación | Acción recomendada |
|-------------|-------------|---------------|------------|-----------|-------------------|
| ✅ `true` | ✅ `true` | `false` | `false` | **Al día** — todo bien | Acceso completo |
| ✅ `true` | ✅ `true` | `false` | ✅ `true` | **Gratis 7 días** — período de gracia | Mostrar días restantes del trial |
| ✅ `true` | ❌ `false` | ✅ `true` | — | **Pago fallido** — suscripción activa pero con deuda | Aviso + pedir actualizar tarjeta |
| ❌ `false` | ❌ `false` | `false` | — | **Pausado** — sin deuda aparente | Pedir reactivar suscripción |
| ❌ `false` | ❌ `false` | ✅ `true` | — | **Pausado por deuda** (agotó reintentos) | Pedir actualizar tarjeta y reactivar |
| ❌ `false` | ❌ `false` | `false` | — | **Sin suscripciones** / canceladas | Ofrecer nueva suscripción |

---

## 5. Overall Status — Estado General del Usuario

> Calculado en [`tokens.py → GET /api/v1/tokens/{customer_id}/dashboard`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/routers/tokens.py#L249-L306)

Además del customer-status, existe un **estado general** (`overall_status`) que combina tarjetas, suscripciones y trial en un solo campo legible. Este es el estado que el frontend/bot usa para tomar decisiones rápidas.

### Los 7 estados del usuario

| `overall_status` | Emoji | Significado | Condición |
|-----------------|-------|-------------|-----------|
| `"active"` | ✅ | Al día, todo funciona | Tiene tarjeta activa + suscripción ACTIVE sin fallos |
| `"trial"` | 🆓 | **Gratis por 7 días** — período de gracia | Suscripción ACTIVE con `trial_ends_at > now` |
| `"retrying"` | ⏳ | Cobro fallido, reintentos pendientes | Suscripción ACTIVE con `failed_attempts > 0` pero no PAUSED aún |
| `"payment_issues"` | ⛔ | Suscripción pausada por deuda | Suscripción PAUSED (agotó 3 reintentos) |
| `"card_expired"` | 💳 | Todas las tarjetas vencidas | Tiene tarjetas pero todas expiradas |
| `"no_card"` | ❌ | Sin tarjetas guardadas | No tiene ninguna tarjeta en DataVault |
| `"no_subscription"` | 📋 | Tiene tarjeta pero sin suscripción | Tarjeta(s) guardada(s) pero sin suscripciones activas |

### Prioridad de evaluación

El sistema evalúa los estados **en este orden** (el primero que aplica gana):

```
1. ¿Tiene tarjetas guardadas?
   └── NO → "no_card"

2. ¿Tiene al menos una tarjeta vigente (no vencida)?
   └── NO → "card_expired"

3. ¿Tiene suscripción en trial (gratis 7 días)?
   └── SÍ → "trial" 🆓

4. ¿Tiene suscripciones PAUSED?
   └── SÍ → "payment_issues"

5. ¿Tiene suscripciones con failed_attempts > 0?
   └── SÍ → "retrying"

6. ¿Tiene suscripciones ACTIVE?
   └── SÍ → "active" ✅

7. Ninguna de las anteriores
   └── "no_subscription"
```

### Respuesta del dashboard

```json
{
  "summary": {
    "status": "trial",
    "message": "Período de gracia activo. Primer cobro programado para 2026-07-27T14:30:00+00:00.",
    "has_cards": true,
    "has_active_card": true,
    "active_subscriptions": 1,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": true,
    "trial_ends_at": "2026-07-27T14:30:00+00:00"
  }
}
```

---

## 6. Gratis por 7 Días — Período de Gracia (Trial)

> Lógica en [`post_payment.py → create_subscription_if_needed()`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/post_payment.py#L196-L323)  
> Constante: `TRIAL_DAYS = 7`

### ¿Qué es?

Cuando un **usuario nuevo** realiza su primer checkout exitoso, recibe **7 días gratis** antes de que se le cobre automáticamente. Durante estos 7 días:

- ✅ Tiene acceso completo al servicio
- ✅ Su suscripción está `ACTIVE`
- ✅ Su `overall_status` es `"trial"`
- ❌ **No se le cobra** — el scheduler no ejecuta cobros hasta que `next_charge_at` se cumpla
- ❌ **No hay reintentos** — aún no hubo primer cobro MIT

### ¿Quién califica para el trial?

| Condición | ¿Trial? |
|-----------|---------|
| **Usuario nuevo** — sin suscripciones previas (ningún estado) **Y** sin tarjetas guardadas | ✅ **Sí, 7 días gratis** |
| **Usuario existente** — tiene al menos 1 suscripción previa (ACTIVE, PAUSED, o CANCELLED) | ❌ No — cobro normal cada 30 días |
| **Usuario existente** — tiene tarjetas guardadas en DataVault | ❌ No — cobro normal cada 30 días |

### Línea de tiempo

```
Día 0                    Día 7                         Día 37
  │                        │                              │
  ▼                        ▼                              ▼
┌─────────────────────┐ ┌──────────────────────────┐ ┌────────────────┐
│   CHECKOUT EXITOSO  │ │  PRIMER COBRO MIT        │ │ SEGUNDO COBRO  │
│                     │ │  (automático)             │ │ MIT            │
│ • Pago: APPROVED    │ │                          │ │                │
│ • Tarjeta guardada  │ │ • Si APPROVED →          │ │ • Ciclo normal │
│   en DataVault      │ │   next_charge += 30 días │ │   cada 30 días │
│ • Suscripción:      │ │                          │ │                │
│   ACTIVE            │ │ • Si DECLINED →          │ └────────────────┘
│ • in_trial = true   │ │   inician reintentos     │
│ • trial_ends_at =   │ │   (backoff 1→3→7 días)   │
│   now + 7 días      │ │                          │
│ • next_charge_at =  │ │ • in_trial = false       │
│   now + 7 días      │ │   (trial terminó)        │
│ • last_charged_at = │ └──────────────────────────┘
│   null (no cobrado) │
└─────────────────────┘

   ◄──── GRATIS 7 DÍAS ────►◄────── COBROS NORMALES ──────►
```

### Campos de la suscripción durante el trial

| Campo | Valor durante trial | Después del trial |
|-------|--------------------|--------------------|
| `status` | `ACTIVE` | `ACTIVE` (si cobro OK) |
| `in_trial` | `true` | `false` |
| `trial_ends_at` | `2026-07-27T14:30:00+00:00` | `2026-07-27T14:30:00+00:00` (se mantiene) |
| `next_charge_at` | `2026-07-27T14:30:00+00:00` (= trial_ends_at) | `2026-08-26T14:30:00+00:00` (+30 días) |
| `last_charged_at` | `null` (nunca cobrado) | `2026-07-27T14:30:00+00:00` |
| `failed_attempts` | `0` | `0` (si exitoso) |
| `overall_status` | `"trial"` | `"active"` |

### ¿Qué pasa al terminar los 7 días?

```
Trial termina (día 7)
        │
        ├── Scheduler ejecuta → MIT a AZUL
        │       │
        │       ├── APPROVED
        │       │       → in_trial = false (trial_ends_at < now)
        │       │       → failed_attempts = 0
        │       │       → next_charge_at += 30 días
        │       │       → overall_status = "active" ✅
        │       │       → Email: primer cobro exitoso
        │       │
        │       └── DECLINED
        │               → in_trial = false
        │               → failed_attempts = 1
        │               → Inician reintentos (1→3→7 días)
        │               → overall_status = "retrying" ⏳
        │               → Si 4 fallos → "payment_issues" ⛔
        │
        └── (el usuario no necesita hacer nada)
```

### Mensaje en el dashboard durante el trial

```json
{
  "summary": {
    "status": "trial",
    "message": "Período de gracia activo. Primer cobro programado para 2026-07-27T14:30:00+00:00."
  }
}
```

---

## 7. Política de Reintentos del Scheduler

> Definida en [`scheduler.py`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/scheduler.py)

El scheduler (APScheduler) ejecuta cada **1 hora** y busca suscripciones ACTIVE con `next_charge_at <= now`.

### Tabla de reintentos

| `failed_attempts` | Espera para reintento | Estado | Notificación |
|-------------------|-----------------------|--------|-------------|
| 0 → 1 | +1 día | `ACTIVE` | Email: cobro fallido |
| 1 → 2 | +3 días | `ACTIVE` | Email: cobro fallido |
| 2 → 3 | +7 días | `ACTIVE` | Email: cobro fallido |
| 3 → 4 | — | **`PAUSED`** ⛔ | Email: suscripción pausada |

### Flujo detallado

```
Scheduler ejecuta (cada hora)
        │
        ├── ¿Tarjeta vencida?
        │       SÍ → PAUSED (sin intentar cobro)
        │       NO ↓
        │
        ├── Enviar cobro MIT a AZUL
        │       │
        │       ├── APPROVED (IsoCode "00")
        │       │       → failed_attempts = 0
        │       │       → next_charge_at += 30 días
        │       │       → Email: cobro exitoso
        │       │
        │       ├── DECLINED (IsoCode "51", "99", etc.)
        │       │       → failed_attempts += 1
        │       │       → ¿failed_attempts > 3?
        │       │           SÍ → PAUSED + email pausada
        │       │           NO → next_charge_at += delay[attempts]
        │       │
        │       └── ERROR (integración)
        │               → NO incrementa failed_attempts
        │               → Se reintentará en la próxima hora
        │
        └── (siguiente suscripción)
```

---

## 8. Ciclo de Vida Completo del Usuario

```
┌─────────────────────────────────────────────────────────────────┐
│                     USUARIO NUEVO                               │
│                                                                 │
│  1. Realiza checkout con tarjeta                                │
│     └── Pago: PENDING → (3DS?) → APPROVED                      │
│                                                                 │
│  2. Post-payment automático:                                    │
│     ├── Tarjeta tokenizada en DataVault                         │
│     ├── Suscripción creada: ACTIVE (trial 7 días)               │
│     ├── Email: recibo de pago                                   │
│     └── Trigger: confirmación de cuenta (auth service)          │
│                                                                 │
│  3. Durante el trial (7 días):                                  │
│     └── in_trial=true, is_active=true, is_current=true          │
│                                                                 │
│  4. Día 8 — primer cobro MIT automático:                        │
│     ├── APPROVED → ciclo normal (cada 30 días)                  │
│     └── DECLINED → inician reintentos                           │
│                                                                 │
│  5. Ciclo recurrente (cada 30 días):                            │
│     ├── APPROVED → todo sigue normal                            │
│     ├── DECLINED 1-3x → reintentos con backoff                  │
│     └── DECLINED 4x → PAUSED automáticamente                   │
│                                                                 │
│  6. Si PAUSED:                                                  │
│     ├── Usuario actualiza tarjeta → POST /resume → ACTIVE       │
│     └── Usuario no hace nada → queda PAUSED indefinidamente     │
│                                                                 │
│  7. Si cancela:                                                 │
│     └── CANCELLED (irreversible, DataVault DELETE)              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 9. Tipos de Pago (`PaymentType`)

> Definido en [`entities.py`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/domain/entities.py#L61-L65)

| Tipo | Valor | Descripción | Quién lo inicia |
|------|-------|-------------|----------------|
| **SALE** | `"SALE"` | Pago único (checkout) | Usuario (CIT) |
| **SERVICE** | `"SERVICE"` | Pago de servicio (luz, agua, etc.) | Usuario (CIT) |
| **RECURRING** | `"RECURRING"` | Cobro de suscripción recurrente | Scheduler (MIT) o usuario (primer cobro CIT) |
| **CLUB** | `"CLUB"` | Cobro tipo club / membresía | Scheduler (MIT) |

### CIT vs MIT

| Indicador | Significado | Cuándo |
|-----------|-------------|--------|
| **CIT** (Cardholder Initiated) | El usuario está presente e inicia la transacción | Checkout, primer cobro de suscripción |
| **MIT** (Merchant Initiated) | El comercio cobra sin presencia del usuario | Cobros recurrentes automáticos del scheduler |

---

## 10. Códigos de Respuesta de AZUL (`IsoCode`)

> Definido en [`entities.py`](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/domain/entities.py#L18-L32)

| IsoCode | Nombre | Significado | Acción |
|---------|--------|-------------|--------|
| `"00"` | APPROVED | ✅ Transacción aprobada | Éxito |
| `"3D"` | THREE_DS_CHALLENGE | 🔐 Requiere challenge 3DS | Redirigir al ACS |
| `"3D2METHOD"` | THREE_DS_METHOD | 🔐 Requiere 3DS Method iframe | Cargar iframe oculto |
| `"51"` | DECLINED_FUNDS | ❌ Fondos insuficientes | Notificar usuario |
| `"08"` | NOT_AUTHENTICATED | ⚠️ ACS no disponible (3DS) | Reintentar sin 3DS |
| `"99"` | ERROR_GENERIC | ⚠️ Error genérico (CVC, tarjeta inválida) | Revisar datos |
| `"63"` | SECURITY_VIOLATION | 🚫 Violación de seguridad | Bloquear tarjeta |
| `""` | UNKNOWN | ❓ Sin código (error pre-procesador) | Revisar logs |

---

## 11. Endpoint: Consultar Estado del Cliente

```
GET /api/v1/recurring/customer-status?customer_id={customer_id}
```

### Ejemplo de respuesta (usuario al día)

```json
{
  "customer_id": "CLI-001",
  "has_subscriptions": true,
  "is_active": true,
  "is_current": true,
  "has_overdue_payment": false,
  "in_trial": false,
  "trial_ends_at": null,
  "total_subscriptions": 1,
  "active_count": 1,
  "paused_count": 0,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "a1b2c3d4-...",
      "description": "Membresía Atlas",
      "amount": 5000,
      "status": "ACTIVE",
      "is_current": true,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 0,
      "next_charge_at": "2026-08-15T14:30:00+00:00",
      "last_charged_at": "2026-07-15T14:30:00+00:00",
      "card_last4": "5872",
      "in_trial": false,
      "trial_ends_at": null
    }
  ]
}
```

### Ejemplo de respuesta (usuario en trial)

```json
{
  "customer_id": "CLI-NEW",
  "has_subscriptions": true,
  "is_active": true,
  "is_current": true,
  "has_overdue_payment": false,
  "in_trial": true,
  "trial_ends_at": "2026-07-27T14:30:00+00:00",
  "total_subscriptions": 1,
  "active_count": 1,
  "paused_count": 0,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "xyz-789-...",
      "description": "Membresía Atlas",
      "amount": 5000,
      "status": "ACTIVE",
      "is_current": true,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 0,
      "next_charge_at": "2026-07-27T14:30:00+00:00",
      "last_charged_at": null,
      "card_last4": "1234",
      "in_trial": true,
      "trial_ends_at": "2026-07-27T14:30:00+00:00"
    }
  ]
}
```

### Ejemplo de respuesta (usuario pausado por deuda)

```json
{
  "customer_id": "CLI-003",
  "has_subscriptions": true,
  "is_active": false,
  "is_current": false,
  "has_overdue_payment": false,
  "in_trial": false,
  "trial_ends_at": null,
  "total_subscriptions": 1,
  "active_count": 0,
  "paused_count": 1,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "c3d4e5f6-...",
      "description": "Membresía Atlas",
      "amount": 5000,
      "status": "PAUSED",
      "is_current": false,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 4,
      "next_charge_at": null,
      "last_charged_at": "2026-05-15T14:30:00+00:00",
      "card_last4": "9876",
      "in_trial": false,
      "trial_ends_at": null
    }
  ]
}
```

---

## 12. Resumen Rápido — Archivos Clave

| Archivo | Responsabilidad |
|---------|----------------|
| [entities.py](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/domain/entities.py) | Enums: `PaymentStatus`, `SubscriptionStatus`, `PaymentType`, `IsoCode` |
| [recurring_service.py](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/recurring_service.py) | CRUD de suscripciones, pause/resume/cancel, `get_customer_status()` |
| [scheduler.py](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/scheduler.py) | Cobros automáticos MIT, reintentos, auto-pause |
| [post_payment.py](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/app/services/post_payment.py) | Acciones post-checkout: crear suscripción, emails, **trial 7 días gratis** |
| [tokens.py (router)](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/routers/tokens.py) | Dashboard del usuario, **`overall_status`** (7 estados), detección de trial |
| [recurring.py (router)](file:///c:/Users/zadkiel/Desktop/azul_pagos_atlas/routers/recurring.py) | Endpoints HTTP: customer-status, create, charge, pause, resume, cancel |

---

*Documentación generada para Atlas Pagos v0.5.0 — Julio 2026*
