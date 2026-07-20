# Endpoint: Estado de Suscripción del Cliente

> **Endpoint:** `GET /api/v1/recurring/customer-status`  
> **Versión:** v1  
> **Fecha:** 2026-07-20  
> **Autenticación:** Ninguna adicional (misma auth del API)

---

## Propósito

Consultar si un cliente está **activo** con su suscripción y **al día** con sus pagos, o si tiene pagos **vencidos/fallidos**.

Este endpoint consolida toda la información de suscripciones de un cliente en una sola respuesta, eliminando la necesidad de consultar múltiples endpoints y calcular el estado manualmente.

---

## Request

### URL

```
GET /api/v1/recurring/customer-status?customer_id={customer_id}
```

### Query Parameters

| Parámetro     | Tipo     | Requerido | Descripción                                 |
|---------------|----------|-----------|---------------------------------------------|
| `customer_id` | `string` | ✅ Sí     | ID del cliente en el sistema (ej: `CLI-001`) |

### Headers

```http
Content-Type: application/json
```

### Ejemplo de Request

```bash
curl -X GET "https://tu-dominio.com/api/v1/recurring/customer-status?customer_id=CLI-001"
```

---

## Response

### HTTP Status Codes

| Código | Significado                        |
|--------|------------------------------------|
| `200`  | OK — respuesta exitosa             |
| `422`  | Validation Error — falta `customer_id` |
| `500`  | Error interno del servidor         |

---

## Campos de Respuesta

### Nivel principal (resumen del cliente)

| Campo                | Tipo      | Descripción                                                                                     |
|----------------------|-----------|-------------------------------------------------------------------------------------------------|
| `customer_id`        | `string`  | ID del cliente consultado                                                                       |
| `has_subscriptions`  | `boolean` | `true` si el cliente tiene al menos una suscripción (de cualquier estado)                       |
| `is_active`          | `boolean` | `true` si tiene al menos una suscripción con estado `ACTIVE`                                    |
| `is_current`         | `boolean` | **⭐ Campo principal** — `true` si está al día con TODOS los pagos (sin deudas ni fallos)       |
| `has_overdue_payment`| `boolean` | `true` si alguna suscripción tiene un pago vencido o con intentos de cobro fallidos             |
| `total_subscriptions`| `integer` | Cantidad total de suscripciones (cualquier estado)                                              |
| `active_count`       | `integer` | Cantidad de suscripciones con estado `ACTIVE`                                                   |
| `paused_count`       | `integer` | Cantidad de suscripciones con estado `PAUSED`                                                   |
| `cancelled_count`    | `integer` | Cantidad de suscripciones con estado `CANCELLED`                                                |
| `subscriptions`      | `array`   | Detalle de cada suscripción individual (ver tabla siguiente)                                    |

### Nivel de suscripción individual (`subscriptions[]`)

| Campo              | Tipo             | Descripción                                                                                |
|--------------------|------------------|--------------------------------------------------------------------------------------------|
| `subscription_id`  | `string`         | UUID de la suscripción                                                                     |
| `description`      | `string`         | Descripción de la suscripción (ej: "Membresía mensual")                                    |
| `amount`           | `integer`        | Monto del cobro en centavos (ej: `5000` = RD$50.00)                                       |
| `status`           | `string`         | Estado actual: `ACTIVE`, `PAUSED`, o `CANCELLED`                                           |
| `is_current`       | `boolean`        | `true` si esta suscripción específica está al día                                          |
| `is_overdue`       | `boolean`        | `true` si esta suscripción tiene pago vencido o fallido                                    |
| `overdue_reason`   | `string`         | Razón del atraso (vacío si está al día). Ej: `"Cobro fallido (2 intento(s)): Fondos insuficientes"` |
| `failed_attempts`  | `integer`        | Número de intentos de cobro fallidos consecutivos                                          |
| `next_charge_at`   | `string \| null` | Fecha del próximo cobro programado (ISO 8601 UTC). `null` si no hay cobro programado       |
| `last_charged_at`  | `string \| null` | Fecha del último cobro exitoso (ISO 8601 UTC). `null` si nunca se ha cobrado               |
| `card_last4`       | `string`         | Últimos 4 dígitos de la tarjeta asociada                                                   |

---

## Lógica de Estados

### Campos clave para tomar decisiones

```
¿El usuario puede usar el servicio?  →  is_active
¿Está al día con los pagos?          →  is_current
¿Tiene deuda pendiente?              →  has_overdue_payment
```

### Tabla de decisión

| `is_active` | `is_current` | `has_overdue_payment` | Significado                                    | Acción sugerida                        |
|-------------|-------------|----------------------|------------------------------------------------|----------------------------------------|
| `true`      | `true`      | `false`              | ✅ Activo y al día                              | Acceso completo                        |
| `true`      | `false`     | `true`               | ⚠️ Activo pero con pago(s) fallido(s)          | Mostrar aviso, pedir actualizar tarjeta |
| `false`     | `false`     | `false`              | ⛔ Pausado (sin deuda detectada)                | Pedir reactivar suscripción            |
| `false`     | `false`     | `true`               | ⛔ Pausado por deuda (3+ fallos)                | Pedir actualizar tarjeta y reactivar   |
| `false`     | `false`     | `false`              | ❌ Sin suscripciones / todas canceladas          | Ofrecer nueva suscripción              |

### ¿Cómo se determina `is_overdue`?

Una suscripción individual se marca como `is_overdue = true` cuando:

1. **Cobros fallidos**: `status == ACTIVE` y `failed_attempts > 0`
   - El scheduler intentó cobrar y falló (tarjeta declinada, fondos insuficientes, etc.)
   - La razón del fallo se incluye en `overdue_reason`

2. **Pago vencido**: `status == ACTIVE` y `next_charge_at < fecha_actual`
   - El cobro ya debería haberse ejecutado pero el scheduler aún no lo ha procesado
   - Esto es transitorio — el scheduler corre cada hora

### Diagrama de estados de suscripción

```
                    POST /recurring
                          │
                          ▼
                       ACTIVE ◄──────── POST /resume
                          │                   ▲
              ┌───────────┴───────────┐       │
              │                       │       │
         Cobro exitoso         3+ fallos / vencida
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

## Ejemplos de Respuesta

### Ejemplo 1: Cliente al día ✅

```json
{
  "customer_id": "CLI-001",
  "has_subscriptions": true,
  "is_active": true,
  "is_current": true,
  "has_overdue_payment": false,
  "total_subscriptions": 1,
  "active_count": 1,
  "paused_count": 0,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "description": "Membresía mensual",
      "amount": 5000,
      "status": "ACTIVE",
      "is_current": true,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 0,
      "next_charge_at": "2026-08-15T14:30:00+00:00",
      "last_charged_at": "2026-07-15T14:30:00+00:00",
      "card_last4": "5872"
    }
  ]
}
```

### Ejemplo 2: Cliente con pago fallido ⚠️

```json
{
  "customer_id": "CLI-002",
  "has_subscriptions": true,
  "is_active": true,
  "is_current": false,
  "has_overdue_payment": true,
  "total_subscriptions": 1,
  "active_count": 1,
  "paused_count": 0,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
      "description": "Plan Premium",
      "amount": 150000,
      "status": "ACTIVE",
      "is_current": false,
      "is_overdue": true,
      "overdue_reason": "Cobro fallido (2 intento(s)): Fondos insuficientes",
      "failed_attempts": 2,
      "next_charge_at": "2026-07-22T14:30:00+00:00",
      "last_charged_at": "2026-06-15T14:30:00+00:00",
      "card_last4": "1234"
    }
  ]
}
```

### Ejemplo 3: Cliente pausado por deuda ⛔

```json
{
  "customer_id": "CLI-003",
  "has_subscriptions": true,
  "is_active": false,
  "is_current": false,
  "has_overdue_payment": false,
  "total_subscriptions": 1,
  "active_count": 0,
  "paused_count": 1,
  "cancelled_count": 0,
  "subscriptions": [
    {
      "subscription_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
      "description": "Membresía mensual",
      "amount": 5000,
      "status": "PAUSED",
      "is_current": false,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 4,
      "next_charge_at": null,
      "last_charged_at": "2026-05-15T14:30:00+00:00",
      "card_last4": "9876"
    }
  ]
}
```

### Ejemplo 4: Cliente sin suscripciones

```json
{
  "customer_id": "CLI-999",
  "has_subscriptions": false,
  "is_active": false,
  "is_current": false,
  "has_overdue_payment": false,
  "total_subscriptions": 0,
  "active_count": 0,
  "paused_count": 0,
  "cancelled_count": 0,
  "subscriptions": []
}
```

### Ejemplo 5: Cliente con múltiples suscripciones (mixto)

```json
{
  "customer_id": "CLI-005",
  "has_subscriptions": true,
  "is_active": true,
  "is_current": false,
  "has_overdue_payment": true,
  "total_subscriptions": 3,
  "active_count": 2,
  "paused_count": 0,
  "cancelled_count": 1,
  "subscriptions": [
    {
      "subscription_id": "aaa-111",
      "description": "Plan Básico",
      "amount": 3000,
      "status": "ACTIVE",
      "is_current": true,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 0,
      "next_charge_at": "2026-08-10T10:00:00+00:00",
      "last_charged_at": "2026-07-10T10:00:00+00:00",
      "card_last4": "4321"
    },
    {
      "subscription_id": "bbb-222",
      "description": "Servicio Premium",
      "amount": 10000,
      "status": "ACTIVE",
      "is_current": false,
      "is_overdue": true,
      "overdue_reason": "Cobro fallido (1 intento(s)): Tarjeta vencida",
      "failed_attempts": 1,
      "next_charge_at": "2026-07-21T10:00:00+00:00",
      "last_charged_at": "2026-06-18T10:00:00+00:00",
      "card_last4": "8765"
    },
    {
      "subscription_id": "ccc-333",
      "description": "Plan Anterior",
      "amount": 2000,
      "status": "CANCELLED",
      "is_current": false,
      "is_overdue": false,
      "overdue_reason": "",
      "failed_attempts": 0,
      "next_charge_at": null,
      "last_charged_at": "2026-03-01T10:00:00+00:00",
      "card_last4": "5555"
    }
  ]
}
```

> **Nota**: `is_current` a nivel global es `false` porque al menos una suscripción activa (`bbb-222`) tiene pago fallido. El cliente tiene acceso parcial — depende de tu lógica de negocio decidir qué hacer.

---

## Integración Frontend

### JavaScript / TypeScript

```typescript
// Tipos
interface SubscriptionStatusDetail {
  subscription_id: string;
  description: string;
  amount: number;           // centavos
  status: "ACTIVE" | "PAUSED" | "CANCELLED";
  is_current: boolean;
  is_overdue: boolean;
  overdue_reason: string;
  failed_attempts: number;
  next_charge_at: string | null;   // ISO 8601
  last_charged_at: string | null;  // ISO 8601
  card_last4: string;
}

interface CustomerStatusResponse {
  customer_id: string;
  has_subscriptions: boolean;
  is_active: boolean;
  is_current: boolean;
  has_overdue_payment: boolean;
  total_subscriptions: number;
  active_count: number;
  paused_count: number;
  cancelled_count: number;
  subscriptions: SubscriptionStatusDetail[];
}

// Función para consultar el estado
async function getCustomerStatus(customerId: string): Promise<CustomerStatusResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/recurring/customer-status?customer_id=${encodeURIComponent(customerId)}`
  );

  if (!response.ok) {
    throw new Error(`Error ${response.status}: ${response.statusText}`);
  }

  return response.json();
}

// Uso
const status = await getCustomerStatus("CLI-001");

if (!status.has_subscriptions) {
  // → Mostrar pantalla de "Suscríbete"
} else if (status.is_current) {
  // → Acceso completo al servicio
} else if (status.has_overdue_payment) {
  // → Mostrar aviso de pago pendiente
  const overdueSubs = status.subscriptions.filter(s => s.is_overdue);
  // → Mostrar detalle de cada suscripción con deuda
} else if (!status.is_active) {
  // → Suscripción pausada/cancelada, ofrecer reactivar
}
```

### React (ejemplo de componente)

```tsx
function SubscriptionBanner({ customerId }: { customerId: string }) {
  const [status, setStatus] = useState<CustomerStatusResponse | null>(null);

  useEffect(() => {
    getCustomerStatus(customerId).then(setStatus);
  }, [customerId]);

  if (!status) return <Spinner />;

  if (!status.has_subscriptions) {
    return <Banner type="info" message="No tienes suscripción activa" />;
  }

  if (status.is_current) {
    return <Banner type="success" message="Tu suscripción está al día ✅" />;
  }

  if (status.has_overdue_payment) {
    const overdue = status.subscriptions.filter(s => s.is_overdue);
    return (
      <Banner type="warning">
        <p>Tienes {overdue.length} pago(s) pendiente(s)</p>
        {overdue.map(s => (
          <p key={s.subscription_id}>
            {s.description}: {s.overdue_reason}
          </p>
        ))}
        <Button onClick={handleUpdateCard}>Actualizar tarjeta</Button>
      </Banner>
    );
  }

  return <Banner type="error" message="Tu suscripción está suspendida" />;
}
```

---

## Formato de Montos

Los montos están en **centavos** (enteros). Para convertir a formato legible:

| `amount` (respuesta) | Moneda | Valor real |
|-----------------------|--------|------------|
| `5000`                | DOP    | RD$50.00   |
| `150000`              | DOP    | RD$1,500.00|
| `1000`                | USD    | US$10.00   |

```typescript
function formatAmount(centavos: number, currency: string = "DOP"): string {
  const valor = centavos / 100;
  const prefix = currency === "USD" ? "US$" : "RD$";
  return `${prefix}${valor.toLocaleString("es-DO", { minimumFractionDigits: 2 })}`;
}
```

---

## Formato de Fechas

Las fechas están en **ISO 8601 UTC** (ej: `2026-07-15T14:30:00+00:00`).

Para mostrar en hora local (República Dominicana = UTC-4):

```typescript
function formatDate(isoString: string | null): string {
  if (!isoString) return "—";
  return new Date(isoString).toLocaleDateString("es-DO", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}
```

---

## Política de Reintentos del Scheduler

Cuando un cobro automático falla, el scheduler reintenta con este calendario:

| `failed_attempts` | Espera hasta el próximo intento | Estado         |
|-------------------|---------------------------------|----------------|
| 1                 | 1 día                           | `ACTIVE`       |
| 2                 | 3 días                          | `ACTIVE`       |
| 3                 | 7 días                          | `ACTIVE`       |
| 4+                | —                               | `PAUSED` ⛔    |

Después del intento 4, la suscripción se pausa automáticamente y el cliente recibe un email de notificación.

---

## Endpoints Relacionados

| Acción                         | Método   | Endpoint                                | Cuándo usarlo                                     |
|--------------------------------|----------|-----------------------------------------|---------------------------------------------------|
| Consultar estado del cliente   | `GET`    | `/api/v1/recurring/customer-status`     | Para saber si está al día o debe                  |
| Listar suscripciones           | `GET`    | `/api/v1/recurring?customer_id=...`     | Para ver todas las suscripciones con detalle completo |
| Ver suscripción específica     | `GET`    | `/api/v1/recurring/{id}`                | Para ver detalle de una suscripción               |
| Historial de cobros            | `GET`    | `/api/v1/recurring/{id}/history`        | Para ver todos los intentos de cobro              |
| Reanudar suscripción pausada   | `POST`   | `/api/v1/recurring/{id}/resume`         | Después de que el cliente actualice su tarjeta    |
| Cobrar manualmente             | `POST`   | `/api/v1/recurring/{id}/charge`         | Para cobrar inmediatamente sin esperar al scheduler|
| Listar tarjetas del cliente    | `GET`    | `/api/v1/tokens/{customer_id}`          | Para ver las tarjetas guardadas                   |
| Registrar nueva tarjeta        | `POST`   | `/api/v1/tokens`                        | Para cambiar la tarjeta de pago                   |

---

## Flujo de Integración Sugerido

```
1. Frontend carga la app
       │
       ▼
2. GET /api/v1/recurring/customer-status?customer_id=CLI-001
       │
       ├── has_subscriptions == false?
       │       → Mostrar pantalla de "Suscríbete"
       │       → POST /api/v1/recurring (crear suscripción)
       │
       ├── is_current == true?
       │       → ✅ Acceso completo
       │       → Mostrar "Próximo cobro: {next_charge_at}"
       │
       ├── has_overdue_payment == true?
       │       → ⚠️ Mostrar aviso con overdue_reason
       │       → Botón "Actualizar tarjeta" → POST /api/v1/tokens
       │       → Botón "Pagar ahora" → POST /api/v1/recurring/{id}/charge
       │
       └── is_active == false (PAUSED)?
               → ⛔ Mostrar "Suscripción suspendida"
               → Botón "Reactivar" → POST /api/v1/recurring/{id}/resume
               → (Opcional) Cobrar inmediato → POST /api/v1/recurring/{id}/charge
```

---

## Origen de Datos

- **Base de datos:** PostgreSQL RDS (`Atlas_User_Service`)
- **Schema:** `pagos`
- **Tabla:** `recurring_payments`
- **Query:** `SELECT * FROM pagos.recurring_payments WHERE customer_id = ? ORDER BY created_at DESC`
- **Latencia esperada:** < 50ms (solo lectura local, no llama a Azul)

---

## Notas Importantes

1. **No llama a Azul** — Este endpoint solo consulta la base de datos local. Es rápido y se puede llamar frecuentemente sin preocupación.

2. **Consistencia** — Los datos se actualizan cuando el scheduler ejecuta cobros (cada hora) o cuando se usan los endpoints de charge/pause/resume/cancel.

3. **Múltiples suscripciones** — Un cliente puede tener varias suscripciones. `is_current` a nivel global es `false` si **cualquiera** de las suscripciones activas tiene deuda.

4. **Suscripciones canceladas** — Se incluyen en la respuesta por trazabilidad pero no afectan `is_active` ni `is_current`.

5. **Campo `amount` en centavos** — Siempre dividir entre 100 para mostrar al usuario. `5000` = RD$50.00.

6. **Swagger UI** — Puedes probar el endpoint interactivamente en `https://tu-dominio.com/docs`.
