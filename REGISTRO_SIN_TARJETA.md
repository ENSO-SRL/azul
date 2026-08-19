# Registro sin Tarjeta — Trial 30 Días

**Sistema:** Atlas Pagos (`pagos.iamatlas.do`)  
**Versión:** 1.0 — Agosto 2026

---

## Descripción

Cuando el frontend registra un usuario nuevo, puede llamar a este endpoint para activar automáticamente un **período de prueba gratuito de 30 días**, sin necesidad de tarjeta de crédito.

Al vencer los 30 días sin haber registrado una tarjeta, el sistema reporta `trial_expired_no_card` y el frontend debe dirigir al usuario a registrar su método de pago en `/checkout`.

---

## Endpoint de Registro

### `POST /api/v1/registration/trial`

**URL producción:**
```
https://pagos.iamatlas.do/api/v1/registration/trial
```

**Autenticación:** Header `X-API-Key` (misma clave que todos los endpoints de la API)

---

### Headers

| Header | Valor |
|--------|-------|
| `X-API-Key` | `atlas-dev-key-2026-colina-del-sol` |
| `Content-Type` | `application/json` |

---

### Request Body

```json
{
  "email": "usuario@ejemplo.com",
  "name": "Juan",
  "last_name": "Pérez",
  "phone": "8096905851",
  "gender": "M",
  "password": "...",
  "confirmPassword": "..."
}
```

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `email` | string | ✅ | Email del usuario — se usa como `customer_id` |
| `name` | string | ✅ | Nombre |
| `last_name` | string | ✅ | Apellido |
| `phone` | string | ❌ | Teléfono (solo para logs) |
| `gender` | string | ❌ | Género (solo para logs) |
| `password` | string | ❌ | Aceptado pero **ignorado** — es para el auth service |
| `confirmPassword` | string | ❌ | Aceptado pero **ignorado** — es para el auth service |

> **Nota:** `password` y `confirmPassword` se incluyen por compatibilidad con el payload del frontend, pero el servicio de pagos **nunca los almacena ni los procesa**.

---

### Respuestas

#### ✅ `201 Created` — Trial creado exitosamente

```json
{
  "status": "created",
  "customer_id": "usuario@ejemplo.com",
  "trial_ends_at": "2026-09-18T10:00:00+00:00",
  "next_charge_at": "2026-09-18T10:00:00+00:00",
  "trial_days": 30,
  "message": "Período de prueba de 30 días creado exitosamente. Primer cobro programado para el 18/09/2026 si se registra una tarjeta antes de esa fecha."
}
```

#### ♻️ `201 Created` — El usuario ya tenía trial activo (idempotente)

```json
{
  "status": "already_active",
  "customer_id": "usuario@ejemplo.com",
  "trial_ends_at": "2026-09-18T10:00:00+00:00",
  "next_charge_at": "2026-09-18T10:00:00+00:00",
  "trial_days": 30,
  "message": "El usuario ya tiene un período de prueba activo. Vence el 18/09/2026."
}
```

> El endpoint es **idempotente**: llamarlo más de una vez con el mismo email no crea duplicados.

#### ❌ `422 Unprocessable Entity` — Validación fallida

```json
{
  "detail": [
    { "loc": ["body", "email"], "msg": "Email inválido", "type": "value_error" }
  ]
}
```

#### ❌ `500 Internal Server Error` — Error de base de datos

```json
{
  "detail": "Error al crear período de prueba: ..."
}
```

---

## Endpoint de Estado

### `GET /api/v1/tokens/status/{customer_id}`

Consulta el estado actual del usuario. `customer_id` puede ser el **email** o el **UUID** del usuario.

**URL:**
```
https://pagos.iamatlas.do/api/v1/tokens/status/usuario@ejemplo.com?API_KEY=atlas-dev-key-2026-colina-del-sol
```

---

### Tabla de Estados (`summary.status`)

| Estado | Cuándo aparece | Acción del frontend |
|--------|---------------|---------------------|
| `trial_no_card` | Trial activo, **sin tarjeta registrada** | Mostrar días restantes del trial |
| `trial_expired_no_card` | Trial expiró, **sin tarjeta registrada** | ⚠️ Redirigir a `/checkout` para agregar tarjeta |
| `trial` | Trial activo, **con tarjeta ya registrada** | Normal — acceso garantizado |
| `active` | Suscripción activa y al día | Normal |
| `payment_issues` | Cobro fallido, suscripción pausada | Pedir actualizar tarjeta |
| `retrying` | Reintentos de cobro pendientes | Avisar al usuario |
| `no_card` | Sin tarjetas y sin suscripción | Redirigir a `/checkout` |
| `card_expired` | Todas las tarjetas vencidas | Pedir nueva tarjeta |
| `no_subscription` | Tiene tarjeta pero sin sub activa | Redirigir a `/checkout` |

---

### Ejemplo de Respuesta — `trial_no_card`

```json
{
  "user_info": {
    "customer_id": "usuario@ejemplo.com",
    "email": "usuario@ejemplo.com",
    "name": "Juan Pérez",
    "found": true
  },
  "cards": [],
  "cards_count": 0,
  "subscriptions": [
    {
      "id": "uuid-sub",
      "status": "ACTIVE",
      "amount": 50000,
      "amount_display": "RD$500.00",
      "in_trial": true,
      "trial_ends_at": "2026-09-18T10:00:00+00:00",
      "next_charge_at": "2026-09-18T10:00:00+00:00",
      "last_charged_at": null,
      "card_last4": ""
    }
  ],
  "summary": {
    "status": "trial_no_card",
    "message": "Período de prueba activo. Tienes acceso gratuito hasta el 2026-09-18. Agrega una tarjeta antes de esa fecha para continuar sin interrupciones.",
    "in_trial": true,
    "trial_ends_at": "2026-09-18T10:00:00+00:00",
    "has_cards": false
  }
}
```

### Ejemplo de Respuesta — `trial_expired_no_card`

```json
{
  "summary": {
    "status": "trial_expired_no_card",
    "message": "Tu período de prueba gratuito expiró. Agrega una tarjeta de crédito para continuar disfrutando de Atlas.",
    "in_trial": false,
    "trial_ends_at": "2026-09-18T10:00:00+00:00",
    "has_cards": false
  }
}
```

---

## Flujo Completo — Paso a Paso

```
1. Usuario se registra en el frontend (ITLA)
         │
         ▼
2. Frontend llama:
   POST /api/v1/registration/trial
   { email, name, last_name, phone, gender, password, confirmPassword }
         │
         ▼
3. Sistema crea suscripción:
   - status = ACTIVE
   - trial_ends_at = hoy + 30 días
   - next_charge_at = hoy + 30 días
   - data_vault_token = "" (sin tarjeta)
   - last_charged_at = null
         │
         ▼
4. Frontend consulta estado periódicamente:
   GET /api/v1/tokens/status/{email}
   → summary.status = "trial_no_card"
         │
         ▼
5. [Pasan 30 días sin tarjeta]
         │
         ▼
6. GET /api/v1/tokens/status/{email}
   → summary.status = "trial_expired_no_card"
         │
         ▼
7. Frontend muestra pantalla: "Tu período de prueba expiró. Agrega tu tarjeta."
         │
         ▼
8. Usuario va a: https://pagos.iamatlas.do/checkout
   Llena formulario con su tarjeta de crédito
         │
         ▼
9. Checkout detecta: ya tiene suscripción ACTIVE (sin tarjeta)
   → NO crea nuevo trial
   → Cobra inmediatamente RD$590.00 (Sale normal)
   → Guarda la tarjeta tokenizada
         │
         ▼
10. GET /api/v1/tokens/status/{email}
    → summary.status = "active"
```

---

## Ejemplos cURL

### Crear trial
```bash
curl -X POST https://pagos.iamatlas.do/api/v1/registration/trial \
  -H "X-API-Key: atlas-dev-key-2026-colina-del-sol" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "juan.perez@ejemplo.com",
    "name": "Juan",
    "last_name": "Pérez",
    "phone": "8096905851",
    "gender": "M",
    "password": "secreto123",
    "confirmPassword": "secreto123"
  }'
```

### Verificar estado
```bash
curl "https://pagos.iamatlas.do/api/v1/tokens/status/juan.perez@ejemplo.com?API_KEY=atlas-dev-key-2026-colina-del-sol"
```

---

## Variables de Entorno

| Variable | Valor por defecto | Descripción |
|----------|-------------------|-------------|
| `REGISTRATION_TRIAL_DAYS` | `30` | Días del período de prueba sin tarjeta |
| `MEMBERSHIP_AMOUNT` | `50000` | Monto del cobro en centavos (RD$500.00) |
| `MEMBERSHIP_ITBIS` | `9000` | ITBIS en centavos (RD$90.00) |

---

## Notas Importantes

- El **scheduler de cobros recurrentes** no cobrará suscripciones sin `data_vault_token`. El usuario en `trial_expired_no_card` **no genera cobros automáticos** — simplemente queda bloqueado hasta que registre tarjeta.
- El `customer_id` que usa este endpoint es el **email del usuario**. El endpoint `/status` soporta búsqueda por email Y por UUID, por lo que ambos formatos funcionan.
- El trial de 30 días de este flujo es **independiente** del trial de 7 días del checkout. Si el usuario llega al checkout después de este trial, **no** recibe otro período de gracia — se le cobra inmediatamente.
