# Apuntes / Pendientes — Creación de suscripciones

> Notas de revisión sobre cómo se crean las suscripciones y un punto a corregir.
> Fecha: 2026-08-10

---

## 1. ⚠️ `card_expiration` se guarda vacío en algunas rutas de creación

### Qué pasa

La guarda de "tarjeta vencida" del scheduler solo actúa **si `card_expiration` tiene valor**:

- `app/services/scheduler.py:97` → `if sub.card_expiration:` … valida `YYYYMM` y, si venció, pone la suscripción en `PAUSED` sin intentar cobrar.

Cuando ese campo se guarda vacío, **esa protección nunca se dispara**: el scheduler igual intenta el cobro MIT. No rompe el flujo (AZUL rechazará la tarjeta vencida y entrará la política de reintentos normal), pero se pierde:

- El **pausado preventivo** (evitar 4 intentos fallidos contra una tarjeta que ya se sabe vencida).
- El mensaje/estado **"actualiza tu tarjeta"** antes de que empiecen los rechazos.

### Dónde se guarda vacío (rutas afectadas)

| Archivo:línea | Función | Valor |
|---|---|---|
| `app/services/post_payment.py:274` | `create_subscription_if_needed()` — rama **usuario nuevo (trial)** | `card_expiration=""` |
| `app/services/post_payment.py:298` | `create_subscription_if_needed()` — rama **usuario existente** | `card_expiration=""` |
| `routers/checkout.py:1740` | Camino **hold-verify** (fallback) → `_SC(expiration="")` pasado a `create_trial_subscription()` | `expiration=""` |

### Dónde SÍ se guarda bien (no tocar)

- **Camino principal de usuario nuevo** en checkout: `routers/checkout.py:1448` `register_card()` → devuelve `SavedCard` con `expiration` real → `create_trial_subscription()` lo persiste (`post_payment.py:382` toma `getattr(saved_card, "expiration", "")`, que aquí **sí** viene lleno).
- **Endpoint directo** `POST /api/v1/recurring`: `app/services/recurring_service.py:158` guarda `card_expiration=expiration` desde el body. Correcto.

### Arreglo sugerido

Propagar la expiración de la tarjeta a las 3 rutas afectadas:

- En `create_subscription_if_needed()` (`post_payment.py`): el `Payment` que llega debería llevar la expiración (o pasarla como argumento) y setear `card_expiration=<YYYYMM>` en ambas ramas en vez de `""`.
- En el camino hold-verify (`checkout.py:1740`): construir el `SavedCard` con la `expiration` real (`exp_azul`) en vez de `""`.

Formato esperado por el scheduler: `YYYYMM` (ej. `202812`).

---

## 2. Caso: crear una suscripción "aparte" (endpoint directo)

Además del alta automática por checkout, existe el alta **manual/directa** por API. **No se comportan igual** — importante tenerlo claro para no esperar un trial donde no lo hay.

### Endpoint directo — `POST /api/v1/recurring`

Definido en `routers/recurring.py:198`, lógica en `app/services/recurring_service.py:70` (`create_subscription`).

| Aspecto | Comportamiento del endpoint directo |
|---|---|
| Cobro | **Cobra el primer pago de inmediato** (CIT `STANDING_ORDER` + `SaveToDataVault=1`). No hay opción de tokenizar sin cobrar. |
| Trial | ❌ **No implementa trial.** Nunca setea `trial_ends_at` → `in_trial` siempre `false`. |
| Lógica nuevo vs. existente | ❌ No existe. Siempre cobra igual, sin distinguir usuario nuevo. |
| Persistencia | Solo guarda la suscripción **si el pago es `APPROVED`** (`recurring_service.py:133`). Si es declinado/3DS pendiente → no persiste. |
| `card_expiration` | ✅ Se guarda correctamente desde el body. |

### Contraste con el alta por checkout

| | Checkout (usuario nuevo) | `POST /api/v1/recurring` |
|---|---|---|
| Al ingresar la tarjeta | **Tokeniza sin cobrar** (`TrxType=CREATE`) | **Cobra de una vez** (CIT) |
| Trial 7 días | ✅ Sí | ❌ No |
| Primer cobro | Automático al día 7 (scheduler, MIT) | Inmediato en la llamada |
| `next_charge_at` | `now + 7 días` | `now + 30 días` |
| `overall_status` resultante | `"trial"` | `"active"` |

### Conclusión / pendiente

Si el objetivo es que **cualquier** vía de alta ofrezca trial para usuarios nuevos, el endpoint directo `POST /api/v1/recurring` **necesitaría** una opción tipo `trial_days` (o `charge_now=false`) que:

1. Tokenice la tarjeta con `CREATE` en vez de cobrar.
2. Setee `trial_ends_at` y `next_charge_at = now + trial_days`.
3. Deje `last_charged_at = None`.

Hoy eso **solo** ocurre por el flujo de checkout, no por el endpoint directo.

---

## Resumen de acciones

- [ ] Propagar `card_expiration` (YYYYMM) en `post_payment.py:274`, `post_payment.py:298` y `checkout.py:1740`.
- [ ] Decidir si `POST /api/v1/recurring` debe soportar trial / usuario nuevo (hoy no).
