# Endpoint: Estado de Tarjetas y Pagos del Usuario

> **Endpoint:** `GET /api/v1/tokens/status/{customer_id}`  
> **Versión:** v1  
> **Fecha:** 2026-08-04  
> **Autenticación:** Ninguna adicional (misma auth del API)

---

## Propósito

Consultar el **estado integral de pago** de un usuario: si tiene tarjeta tokenizada, si las tarjetas están vigentes o vencidas, el estado de sus suscripciones, los últimos pagos realizados, y un **resumen general** que indica de un vistazo si el usuario está al día o tiene problemas.

> [!IMPORTANT]
> Este es el endpoint principal para saber si un usuario **tiene tarjeta tokenizada o no**. Usa el campo `summary.has_cards` para determinarlo.

### ¿Cuándo usar este endpoint vs `/recurring/customer-status`?

| Pregunta | Endpoint a usar |
|---|---|
| ¿El usuario tiene tarjeta tokenizada? | ✅ **Este** (`/tokens/status/`) |
| ¿La tarjeta está vencida? | ✅ **Este** (`/tokens/status/`) |
| ¿El usuario está al día con suscripciones? | Ambos sirven |
| ¿Detalle granular por suscripción (`is_overdue`, `overdue_reason`)? | `/recurring/customer-status` |
| Vista rápida general del usuario (tarjetas + suscripciones + pagos)? | ✅ **Este** (`/tokens/status/`) |

---

## Request

### URL

```
GET /api/v1/tokens/status/{customer_id}
```

### Path Parameters

| Parámetro      | Tipo     | Requerido | Descripción                                                      |
|----------------|----------|-----------|------------------------------------------------------------------|
| `customer_id`  | `string` | ✅ Sí     | UUID del usuario o correo electrónico. Se busca en `public.users` por `uuid` o `email`. |

### Headers

```http
Content-Type: application/json
```

### Ejemplo de Request

```bash
# Por UUID del usuario
curl -X GET "https://tu-dominio.com/api/v1/tokens/status/550e8400-e29b-41d4-a716-446655440000"

# Por correo electrónico
curl -X GET "https://tu-dominio.com/api/v1/tokens/status/juan@ejemplo.com"
```

---

## Response

### HTTP Status Codes

| Código | Significado                            |
|--------|----------------------------------------|
| `200`  | OK — respuesta exitosa                 |
| `422`  | Validation Error — falta `customer_id` |
| `500`  | Error interno del servidor             |

> [!NOTE]
> Este endpoint **siempre retorna 200** aunque el usuario no tenga tarjetas ni suscripciones. En ese caso retorna `summary.status = "no_card"` con listas vacías. No devuelve 404.

---

## Campos de Respuesta

### Nivel principal

| Campo                | Tipo     | Descripción                                              |
|----------------------|----------|----------------------------------------------------------|
| `user_info`          | `object` | Datos básicos del usuario desde `public.users`           |
| `cards`              | `array`  | Lista de tarjetas tokenizadas guardadas                  |
| `cards_count`        | `integer`| Cantidad total de tarjetas                               |
| `subscriptions`      | `array`  | Lista de suscripciones recurrentes                       |
| `subscriptions_count`| `integer`| Cantidad total de suscripciones                          |
| `recent_payments`    | `array`  | Últimos 10 pagos del usuario                             |
| `summary`            | `object` | **⭐ Resumen general** — el campo más importante         |

---

### `user_info` — Datos del usuario

| Campo         | Tipo      | Descripción                                                    |
|---------------|-----------|----------------------------------------------------------------|
| `customer_id` | `string`  | El ID que se pasó en la URL                                    |
| `email`       | `string`  | Correo del usuario (vacío si no se encontró en `public.users`) |
| `name`        | `string`  | Nombre completo del usuario                                    |
| `found`       | `boolean` | `true` si el usuario existe en `public.users`                  |

---

### `cards[]` — Tarjetas tokenizadas

| Campo                | Tipo      | Descripción                                              |
|----------------------|-----------|----------------------------------------------------------|
| `id`                 | `string`  | UUID de la tarjeta en BD                                 |
| `card_brand`         | `string`  | Marca de la tarjeta: `VISA`, `MASTERCARD`, etc.          |
| `card_last4`         | `string`  | Últimos 4 dígitos de la tarjeta (ej: `"5872"`)           |
| `expiration`         | `string`  | Fecha de expiración formato `YYYYMM` (ej: `"202812"`)   |
| `expiration_display` | `string`  | Fecha de expiración visual `MM/AA` (ej: `"12/28"`)       |
| `is_default`         | `boolean` | `true` si es la tarjeta predeterminada de cobro          |
| `is_expired`         | `boolean` | `true` si la tarjeta ya venció                           |
| `status`             | `string`  | `"active"` o `"expired"`                                 |
| `created_at`         | `string`  | Fecha ISO 8601 de cuándo se tokenizó                     |

---

### `subscriptions[]` — Suscripciones recurrentes

| Campo                 | Tipo             | Descripción                                                 |
|-----------------------|------------------|-------------------------------------------------------------|
| `id`                  | `string`         | UUID de la suscripción                                      |
| `status`              | `string`         | Estado: `ACTIVE`, `PAUSED`, `CANCELLED`                     |
| `amount`              | `integer`        | Monto en centavos                                           |
| `amount_display`      | `string`         | Monto formateado (ej: `"RD$500.00"`)                        |
| `frequency_days`      | `integer`        | Frecuencia de cobro en días (ej: `30`)                      |
| `description`         | `string`         | Descripción de la suscripción                               |
| `card_last4`          | `string`         | Últimos 4 dígitos de la tarjeta asociada                    |
| `card_brand`          | `string`         | Marca de la tarjeta asociada                                |
| `next_charge_at`      | `string \| null` | Próximo cobro programado (ISO 8601)                         |
| `last_charged_at`     | `string \| null` | Último cobro exitoso (ISO 8601)                             |
| `failed_attempts`     | `integer`        | Intentos de cobro fallidos consecutivos                     |
| `last_failure_reason` | `string`         | Razón del último fallo (vacío si no ha fallado)             |
| `in_trial`            | `boolean`        | `true` si está en período de gracia                         |
| `trial_ends_at`       | `string \| null` | Fecha en que termina el período de gracia (ISO 8601)        |
| `created_at`          | `string`         | Fecha de creación de la suscripción                         |

---

### `recent_payments[]` — Últimos 10 pagos

| Campo              | Tipo     | Descripción                                          |
|--------------------|----------|------------------------------------------------------|
| `id`               | `string` | UUID del pago                                        |
| `status`           | `string` | Estado: `APPROVED`, `DECLINED`, `ERROR`, etc.        |
| `amount`           | `integer`| Monto en centavos                                    |
| `amount_display`   | `string` | Monto formateado (ej: `"RD$500.00"`)                 |
| `iso_code`         | `string` | Código ISO de respuesta de Azul (ej: `"00"`, `"51"`) |
| `response_message` | `string` | Mensaje descriptivo del resultado                    |
| `card_last4`       | `string` | Últimos 4 dígitos de la tarjeta usada                |
| `payment_type`     | `string` | Tipo de pago                                         |
| `created_at`       | `string` | Fecha del pago (ISO 8601)                            |

---

### `summary` — ⭐ Resumen general del usuario

Este es el campo más importante. Indica de un vistazo el estado general del usuario.

| Campo                   | Tipo      | Descripción                                                        |
|-------------------------|-----------|--------------------------------------------------------------------|
| `status`                | `string`  | **Estado general** — ver tabla de estados abajo                    |
| `message`               | `string`  | Mensaje legible en español describiendo el estado                  |
| `has_cards`             | `boolean` | **`true` si el usuario tiene al menos una tarjeta tokenizada**     |
| `has_active_card`       | `boolean` | `true` si tiene al menos una tarjeta no vencida                    |
| `active_subscriptions`  | `integer` | Cantidad de suscripciones con estado `ACTIVE`                      |
| `paused_subscriptions`  | `integer` | Cantidad de suscripciones con estado `PAUSED`                      |
| `failing_subscriptions` | `integer` | Cantidad de suscripciones con reintentos pendientes                |
| `in_trial`              | `boolean` | `true` si alguna suscripción está en período de gracia             |
| `trial_ends_at`         | `string \| null` | Fecha en que termina el trial (ISO 8601)                    |

---

## Estados del `summary.status`

El campo `summary.status` es un indicador general calculado en cascada. El sistema evalúa las condiciones en orden y asigna el **primer** estado que coincida:

| Prioridad | Estado            | Condición                                        | Significado                                        |
|-----------|-------------------|--------------------------------------------------|----------------------------------------------------|
| 1         | `no_card`         | No tiene ninguna tarjeta guardada                | ❌ El usuario NO tiene tarjeta tokenizada           |
| 2         | `card_expired`    | Tiene tarjetas pero TODAS están vencidas         | ⚠️ Tarjeta(s) vencida(s), no se puede cobrar       |
| 3         | `trial`           | Tiene suscripción activa en período de gracia    | 🕐 Trial activo, primer cobro pendiente            |
| 4         | `payment_issues`  | Tiene suscripción(es) pausada(s) por fallos      | 🔴 Problemas de pago, suscripción pausada          |
| 5         | `retrying`        | Tiene suscripción(es) con reintentos pendientes  | 🟡 Cobros fallando, reintentando automáticamente   |
| 6         | `active`          | Tiene suscripción(es) activa(s) al día           | ✅ Todo al día                                     |
| 7         | `no_subscription` | Tiene tarjeta(s) pero sin suscripciones activas  | 📋 Tarjeta guardada pero sin servicio activo       |

### Diagrama de evaluación

```
                    ¿Tiene tarjetas?
                          │
                    NO ───┤─── SÍ
                    │           │
              "no_card"    ¿Alguna activa (no vencida)?
                                │
                          NO ───┤─── SÍ
                          │           │
                   "card_expired"  ¿En trial?
                                      │
                                SÍ ───┤─── NO
                                │           │
                           "trial"    ¿Suscripciones pausadas?
                                            │
                                      SÍ ───┤─── NO
                                      │           │
                              "payment_    ¿Reintentos pendientes?
                               issues"           │
                                            SÍ ───┤─── NO
                                            │           │
                                       "retrying"  ¿Suscripciones activas?
                                                        │
                                                  SÍ ───┤─── NO
                                                  │           │
                                             "active"   "no_subscription"
```

---

## Tabla de decisión para el frontend

| `summary.status`  | ¿Tiene tarjeta? | ¿Puede cobrar? | Acción sugerida                                    |
|--------------------|-----------------|-----------------|---------------------------------------------------|
| `no_card`          | ❌ No           | ❌ No           | Mostrar flujo de registro de tarjeta              |
| `card_expired`     | ✅ Sí (vencida) | ❌ No           | Pedir actualizar/registrar nueva tarjeta          |
| `trial`            | ✅ Sí           | ✅ Sí           | Mostrar días restantes del trial                  |
| `payment_issues`   | ✅ Sí           | ⚠️ Pausado      | Alerta: pedir actualizar tarjeta o contactar soporte |
| `retrying`         | ✅ Sí           | ⚠️ Reintentando | Aviso: el sistema está reintentando cobrar        |
| `active`           | ✅ Sí           | ✅ Sí           | Todo bien — acceso completo                       |
| `no_subscription`  | ✅ Sí           | ✅ (sin uso)    | Ofrecer crear suscripción / seleccionar plan      |

---

## Ejemplos de Respuesta

### Ejemplo 1: Usuario al día ✅

```json
{
  "user_info": {
    "customer_id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "juan@ejemplo.com",
    "name": "Juan Pérez",
    "found": true
  },
  "cards": [
    {
      "id": "card-uuid-001",
      "card_brand": "VISA",
      "card_last4": "5872",
      "expiration": "202812",
      "expiration_display": "12/28",
      "is_default": true,
      "is_expired": false,
      "status": "active",
      "created_at": "2026-06-15T10:30:00+00:00"
    }
  ],
  "cards_count": 1,
  "subscriptions": [
    {
      "id": "sub-uuid-001",
      "status": "ACTIVE",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "frequency_days": 30,
      "description": "Membresía mensual",
      "card_last4": "5872",
      "card_brand": "VISA",
      "next_charge_at": "2026-09-01T14:30:00+00:00",
      "last_charged_at": "2026-08-01T14:30:00+00:00",
      "failed_attempts": 0,
      "last_failure_reason": "",
      "in_trial": false,
      "trial_ends_at": null,
      "created_at": "2026-06-15T10:30:00+00:00"
    }
  ],
  "subscriptions_count": 1,
  "recent_payments": [
    {
      "id": "pay-uuid-001",
      "status": "APPROVED",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "iso_code": "00",
      "response_message": "APROBADA",
      "card_last4": "5872",
      "payment_type": "MIT",
      "created_at": "2026-08-01T14:30:00+00:00"
    }
  ],
  "summary": {
    "status": "active",
    "message": "Todo al día. 1 suscripción(es) activa(s).",
    "has_cards": true,
    "has_active_card": true,
    "active_subscriptions": 1,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": false,
    "trial_ends_at": null
  }
}
```

### Ejemplo 2: Usuario SIN tarjeta tokenizada ❌

```json
{
  "user_info": {
    "customer_id": "nuevo-usuario-uuid",
    "email": "maria@ejemplo.com",
    "name": "María García",
    "found": true
  },
  "cards": [],
  "cards_count": 0,
  "subscriptions": [],
  "subscriptions_count": 0,
  "recent_payments": [],
  "summary": {
    "status": "no_card",
    "message": "El usuario no tiene tarjetas guardadas.",
    "has_cards": false,
    "has_active_card": false,
    "active_subscriptions": 0,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": false,
    "trial_ends_at": null
  }
}
```

### Ejemplo 3: Usuario en período de gracia (trial) 🕐

```json
{
  "user_info": {
    "customer_id": "trial-user-uuid",
    "email": "carlos@ejemplo.com",
    "name": "Carlos Rodríguez",
    "found": true
  },
  "cards": [
    {
      "id": "card-uuid-002",
      "card_brand": "MASTERCARD",
      "card_last4": "1234",
      "expiration": "202912",
      "expiration_display": "12/29",
      "is_default": true,
      "is_expired": false,
      "status": "active",
      "created_at": "2026-08-01T09:00:00+00:00"
    }
  ],
  "cards_count": 1,
  "subscriptions": [
    {
      "id": "sub-uuid-002",
      "status": "ACTIVE",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "frequency_days": 30,
      "description": "Plan mensual",
      "card_last4": "1234",
      "card_brand": "MASTERCARD",
      "next_charge_at": "2026-08-08T09:00:00+00:00",
      "last_charged_at": null,
      "failed_attempts": 0,
      "last_failure_reason": "",
      "in_trial": true,
      "trial_ends_at": "2026-08-08T09:00:00+00:00",
      "created_at": "2026-08-01T09:00:00+00:00"
    }
  ],
  "subscriptions_count": 1,
  "recent_payments": [],
  "summary": {
    "status": "trial",
    "message": "Período de gracia activo. Primer cobro programado para 2026-08-08T09:00:00+00:00.",
    "has_cards": true,
    "has_active_card": true,
    "active_subscriptions": 1,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": true,
    "trial_ends_at": "2026-08-08T09:00:00+00:00"
  }
}
```

### Ejemplo 4: Tarjeta vencida ⚠️

```json
{
  "user_info": {
    "customer_id": "expired-card-uuid",
    "email": "ana@ejemplo.com",
    "name": "Ana Martínez",
    "found": true
  },
  "cards": [
    {
      "id": "card-uuid-003",
      "card_brand": "VISA",
      "card_last4": "9999",
      "expiration": "202501",
      "expiration_display": "01/25",
      "is_default": true,
      "is_expired": true,
      "status": "expired",
      "created_at": "2024-01-10T12:00:00+00:00"
    }
  ],
  "cards_count": 1,
  "subscriptions": [],
  "subscriptions_count": 0,
  "recent_payments": [],
  "summary": {
    "status": "card_expired",
    "message": "Todas las tarjetas del usuario están vencidas.",
    "has_cards": true,
    "has_active_card": false,
    "active_subscriptions": 0,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": false,
    "trial_ends_at": null
  }
}
```

### Ejemplo 5: Problemas de pago 🔴

```json
{
  "user_info": {
    "customer_id": "payment-issue-uuid",
    "email": "pedro@ejemplo.com",
    "name": "Pedro Sánchez",
    "found": true
  },
  "cards": [
    {
      "id": "card-uuid-004",
      "card_brand": "VISA",
      "card_last4": "4321",
      "expiration": "202812",
      "expiration_display": "12/28",
      "is_default": true,
      "is_expired": false,
      "status": "active",
      "created_at": "2026-05-20T08:00:00+00:00"
    }
  ],
  "cards_count": 1,
  "subscriptions": [
    {
      "id": "sub-uuid-003",
      "status": "PAUSED",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "frequency_days": 30,
      "description": "Membresía mensual",
      "card_last4": "4321",
      "card_brand": "VISA",
      "next_charge_at": null,
      "last_charged_at": "2026-07-01T14:00:00+00:00",
      "failed_attempts": 3,
      "last_failure_reason": "Fondos insuficientes",
      "in_trial": false,
      "trial_ends_at": null,
      "created_at": "2026-05-20T08:00:00+00:00"
    }
  ],
  "subscriptions_count": 1,
  "recent_payments": [
    {
      "id": "pay-uuid-fail",
      "status": "DECLINED",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "iso_code": "51",
      "response_message": "Fondos insuficientes",
      "card_last4": "4321",
      "payment_type": "MIT",
      "created_at": "2026-08-01T14:00:00+00:00"
    }
  ],
  "summary": {
    "status": "payment_issues",
    "message": "1 suscripción(es) pausada(s) por fallos de cobro. Razón(es): Fondos insuficientes.",
    "has_cards": true,
    "has_active_card": true,
    "active_subscriptions": 0,
    "paused_subscriptions": 1,
    "failing_subscriptions": 0,
    "in_trial": false,
    "trial_ends_at": null
  }
}
```

### Ejemplo 6: Tarjeta sin suscripción 📋

```json
{
  "user_info": {
    "customer_id": "card-only-uuid",
    "email": "luis@ejemplo.com",
    "name": "Luis Fernández",
    "found": true
  },
  "cards": [
    {
      "id": "card-uuid-005",
      "card_brand": "MASTERCARD",
      "card_last4": "7777",
      "expiration": "202912",
      "expiration_display": "12/29",
      "is_default": true,
      "is_expired": false,
      "status": "active",
      "created_at": "2026-07-01T10:00:00+00:00"
    }
  ],
  "cards_count": 1,
  "subscriptions": [],
  "subscriptions_count": 0,
  "recent_payments": [],
  "summary": {
    "status": "no_subscription",
    "message": "El usuario tiene tarjeta(s) pero no tiene suscripciones activas.",
    "has_cards": true,
    "has_active_card": true,
    "active_subscriptions": 0,
    "paused_subscriptions": 0,
    "failing_subscriptions": 0,
    "in_trial": false,
    "trial_ends_at": null
  }
}
```

---

## Ejemplo de uso en JavaScript (Frontend)

```javascript
async function getUserPaymentStatus(customerId) {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/tokens/status/${encodeURIComponent(customerId)}`
  );
  const data = await response.json();
  return data;
}

// --- Uso práctico ---

const status = await getUserPaymentStatus("juan@ejemplo.com");

// ¿Tiene tarjeta tokenizada?
if (!status.summary.has_cards) {
  showCardRegistrationFlow();
  return;
}

// ¿La tarjeta está vencida?
if (!status.summary.has_active_card) {
  showUpdateCardPrompt();
  return;
}

// Decisión por estado general
switch (status.summary.status) {
  case "active":
    showDashboard();
    break;
  case "trial":
    showTrialBanner(status.summary.trial_ends_at);
    break;
  case "payment_issues":
    showPaymentAlert(status.summary.message);
    break;
  case "retrying":
    showRetryingNotice();
    break;
  case "no_subscription":
    showSubscriptionPlans();
    break;
  case "card_expired":
    showUpdateCardPrompt();
    break;
  case "no_card":
    showCardRegistrationFlow();
    break;
}
```

---

## Fuentes de datos

El endpoint cruza información de 4 tablas:

| Tabla                       | Datos que aporta                              |
|-----------------------------|-----------------------------------------------|
| `public.users`              | Nombre y email del usuario                    |
| `pagos.saved_cards`         | Tarjetas tokenizadas (DataVault)              |
| `pagos.recurring_payments`  | Suscripciones recurrentes                     |
| `pagos.payments`            | Historial de transacciones                    |

> [!NOTE]
> Si `public.users` no es accesible (permisos de esquema), el endpoint **no falla** — retorna `user_info.found = false` con email y nombre vacíos. El resto de la data de tarjetas, suscripciones y pagos se retorna normalmente.

---

## Resumen de endpoints de estado disponibles

| Endpoint | Propósito | Campos clave |
|---|---|---|
| `GET /api/v1/tokens/status/{customer_id}` | **Radiografía completa**: tarjetas + suscripciones + pagos + resumen | `summary.has_cards`, `summary.status`, `cards[]`, `subscriptions[]` |
| `GET /api/v1/recurring/customer-status?customer_id=X` | **Foco en suscripciones**: detalle granular de cada suscripción | `is_active`, `is_current`, `has_overdue_payment`, `subscriptions[].is_overdue` |
| `GET /api/v1/tokens/by-email/{email}` | **Solo tarjetas** (datos seguros para visual): sin tokens expuestos | `card_brand`, `card_last4`, `token_masked` |
| `GET /api/v1/tokens/{customer_id}` | **Tarjetas completas** (incluye token DataVault real) | `token`, `card_brand`, `card_last4` |
