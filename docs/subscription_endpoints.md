# Endpoints de suscripciones

Referencia de todos los endpoints relacionados con información de suscripción
(alta, consulta de estado, cobro, pausa/reanudación, cancelación, consentimiento
e historial). Todos requieren header `X-API-Key` vía `Depends(require_api_key)`
(`app/main.py:166,169,170` — aplicado globalmente a `recurring_router` y
`tokens_router`).

## `routers/recurring.py` — prefix `/api/v1/recurring`

| Método | Path | Función | Línea |
|---|---|---|---|
| POST | `/api/v1/recurring` | `create_subscription` | `routers/recurring.py:204` |
| GET | `/api/v1/recurring` | `list_subscriptions` | `routers/recurring.py:249` |
| GET | `/api/v1/recurring/customer-status` | `get_customer_status` | `routers/recurring.py:263` |
| GET | `/api/v1/recurring/{recurring_id}` | `get_subscription` | `routers/recurring.py:284` |
| POST | `/api/v1/recurring/{recurring_id}/charge` | `charge_subscription` | `routers/recurring.py:299` |
| POST | `/api/v1/recurring/{recurring_id}/pause` | `pause_subscription` | `routers/recurring.py:326` |
| POST | `/api/v1/recurring/{recurring_id}/resume` | `resume_subscription` | `routers/recurring.py:345` |
| DELETE | `/api/v1/recurring/{recurring_id}` | `cancel_subscription` | `routers/recurring.py:362` |
| POST | `/api/v1/recurring/{recurring_id}/consent` | `record_consent` | `routers/recurring.py:385` |
| GET | `/api/v1/recurring/{recurring_id}/consent` | `get_consent` | `routers/recurring.py:424` |
| GET | `/api/v1/recurring/{recurring_id}/history` | `get_subscription_history` | `routers/recurring.py:443` |

### `POST /api/v1/recurring` — Crear suscripción

Ejecuta el primer cobro como CIT `STANDING_ORDER` (le avisa a Visa/MC que
habrá cobros futuros merchant-initiated, mejora tasas de aprobación), tokeniza
la tarjeta con DataVault y crea el registro recurrente.

- Body: `CreateSubscriptionRequest` (`recurring.py:54`) — `customer_id`,
  `amount`/`itbis` (centavos), `card_number`, `expiration` (`YYYYMM`), `cvc`,
  `frequency_days` (default 30), `description`, `currency` (default `DOP`),
  `cardholder_name`, `cardholder_email`, `auth_mode` (`splitit`|`3dsecure`),
  `browser_info` (obligatorio si `auth_mode=3dsecure`).
- Respuesta: `201` + `SubscriptionResponse` (`recurring.py:80`), incluye
  además `initial_payment_id` / `initial_payment_status` del primer cobro.
- Errores: `503` si falla la integración con Azul, `400` en cualquier otro error.

### `GET /api/v1/recurring?customer_id=...` — Listar suscripciones

Retorna `list[SubscriptionResponse]` con **todas** las suscripciones del
cliente, sin filtrar por estado.

### `GET /api/v1/recurring/customer-status?customer_id=...` — Estado consolidado

El endpoint principal para consultar si un cliente está al día. Documentado
en detalle en `docs/CUSTOMER_STATUS_ENDPOINT.md`. Respuesta:
`CustomerStatusResponse` (`recurring.py:154`):

- `is_active` — `true` si tiene al menos una suscripción `ACTIVE`.
- `is_current` — `true` si está al día con **todos** los pagos (ningún cobro
  pendiente/fallido).
- `has_overdue_payment` — `true` si alguna suscripción tiene pago vencido o
  con intentos fallidos.
- `in_trial` / `trial_ends_at` — si hay una suscripción activa en período de
  gracia (7 días).
- `total_subscriptions`, `active_count`, `paused_count`, `cancelled_count`.
- `subscriptions: list[SubscriptionStatusDetail]` (`recurring.py:138`) — detalle
  por suscripción, incluyendo `is_current`, `is_overdue`, `overdue_reason`.

Este es el que el equipo de auth mencionó como candidato para el gate de
login (`is_active`/`is_current` en la tabla de subscripciones).

### `GET /api/v1/recurring/{recurring_id}` — Detalle de una suscripción

Retorna `SubscriptionResponse` o `404` si no existe.

### `POST /api/v1/recurring/{recurring_id}/charge` — Cobro manual (MIT)

Cobra usando el token DataVault almacenado, sin el cliente presente
(`merchantInitiatedIndicator: STANDING_ORDER`). Respuesta: `ChargeResponse`
(`recurring.py:103`). `404` si no existe la suscripción, `503` si falla Azul.

### `POST /api/v1/recurring/{recurring_id}/pause` y `/resume`

Pausan/reanudan sin tocar el token DataVault (se conserva para reactivar más
tarde). `resume` resetea `failed_attempts`. `404` si no existe.

### `DELETE /api/v1/recurring/{recurring_id}` — Cancelar

Cancela la suscripción y ejecuta `TrxType=DELETE` en DataVault (best-effort,
cumplimiento GDPR — si el DELETE falla, la suscripción se cancela igual y el
fallo queda solo en logs).

### `POST` / `GET /api/v1/recurring/{recurring_id}/consent` — Consentimiento

Visa/MC exigen evidencia documentada de que el cliente autorizó los cobros
recurrentes. `POST` debe llamarse **después del primer cobro exitoso**,
guarda `consent_text`, `ip_address` (o la IP de la request si no se envía),
`user_agent` y timestamp UTC. `GET` recupera el registro (`404` si no existe).

### `GET /api/v1/recurring/{recurring_id}/history` — Historial de cobros

Retorna `list[TransactionHistoryItem]` (`recurring.py:170`) con todos los
intentos de cobro (éxito y fallo) de la suscripción, orden descendente por
fecha. Útil para reconciliación y para mostrarle al cliente su historial.

## Endpoint relacionado — `routers/tokens.py`

### `GET /api/v1/tokens/status/{customer_id}` — Estado integral de pago

`get_user_payment_status` (`routers/tokens.py:100`). Cruza tarjetas guardadas
(`pagos.saved_cards`), suscripciones (`pagos.recurring_payments`), los
últimos 10 pagos (`pagos.payments`) y datos del usuario (`public.users`) en
un solo JSON:

```
{
  "user_info": {...},
  "cards": [...], "cards_count": N,
  "subscriptions": [...], "subscriptions_count": N,
  "recent_payments": [...],
  "summary": {
    "status": "no_card" | "card_expired" | "trial" | "payment_issues"
             | "retrying" | "active" | "no_subscription",
    "message": "...",
    "has_cards": bool, "has_active_card": bool,
    "active_subscriptions": N, "paused_subscriptions": N,
    "failing_subscriptions": N, "in_trial": bool, "trial_ends_at": str|null
  }
}
```

La prioridad de `summary.status` es: sin tarjetas → tarjetas vencidas → en
trial → pausadas por fallo → con reintentos pendientes → activas → sin
suscripción (`routers/tokens.py:257-286`).

> **Nota:** `docs/USER_STATES.md` (líneas 161 y 564) referencia este endpoint
> como `GET /api/v1/tokens/{customer_id}/dashboard` — ese path está
> desactualizado, la ruta real es `/api/v1/tokens/status/{customer_id}`.

## Modelos y enums relacionados

- Schemas de request/response: todos en `routers/recurring.py` (líneas 42-177).
- `SubscriptionStatus` (`ACTIVE` / `PAUSED` / `CANCELLED`) —
  `app/domain/entities.py:68-71`.
- `PaymentStatus` — `app/domain/entities.py:50-58`.
- `PaymentType` — `app/domain/entities.py:61-65`.

## Documentación existente relacionada

- `docs/CUSTOMER_STATUS_ENDPOINT.md` — referencia completa de
  `GET /recurring/customer-status`: campos, tabla de decisión
  `is_active`/`is_current`/`has_overdue_payment`, lógica de vencimiento,
  ejemplos de integración JS/TS.
- `docs/USER_STATES.md` — doc más amplia de la máquina de estados: enums,
  estado consolidado del cliente, los 7 valores de `summary.status`,
  mecánica del período de gracia (7 días), política de reintentos del
  scheduler (1→3→7 días, luego auto-pausa), con punteros a
  `recurring_service.py`, `scheduler.py`, `post_payment.py`, `tokens.py`.
