# Azul Endpoints — Reference

URLs y endpoints de la API de Azul Payment Gateway.

## Environments

| Entorno | Base URL |
|---------|----------|
| **Sandbox** (pruebas) | `https://pruebas.azul.com.do/webservices/JSON/default.aspx` |
| **Production** | `https://pagos.azul.com.do/WebServices/JSON/default.aspx` |

Controla el entorno con la variable de entorno `AZUL_ENV`:
```bash
AZUL_ENV=sandbox      # default — usa pruebas.azul.com.do
AZUL_ENV=production   # usa pagos.azul.com.do
```

---

## Endpoints JSON API

Todos los endpoints usan el mismo método: `POST` con `Content-Type: application/json` y mTLS.

| Endpoint | `TrxType` | Descripción |
|----------|-----------|-------------|
| `{base_url}` | `Sale` | Cobro con tarjeta completa o DataVault token |
| `{base_url}` | `CREATE` | Tokenizar tarjeta sin cobrar (DataVault) |
| `{base_url}` | `DELETE` | Eliminar token de DataVault |
| `{base_url}` | `Hold` | Reserva de fondos (pre-autorización) |
| `{base_url}` | `Post` | Capturar un Hold previo |
| `{base_url}` | `Void` | Anular transacción (mismo día) |
| `{base_url}` | `Refund` | Devolución parcial o total |

---

## Endpoints 3DS 2.0 (solo producción)

| Paso | URL | Descripción |
|------|-----|-------------|
| Paso 3 — Evaluación | `https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedsmethod` | Enviar ThreeDSMethodData al ACS del emisor |
| Paso 8 — Completar | `https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedschallenge` | Completar autorización tras el challenge |

> **Nota**: El flujo 3DS 2.0 completo requiere 8 pasos con redirects al ACS del banco emisor.
> Está planificado para Fase 3. Contactar `solucionesintegradas@bpd.com.do` para activar 3DS en el merchant.

---

## Headers requeridos en cada request

```http
Content-Type: application/json
Auth1: <valor proporcionado por Azul>
Auth2: <valor proporcionado por Azul>
```

El mTLS se configura en el cliente HTTP (certificado + clave privada) — no va en headers.

---

## Nuestros endpoints (Atlas API)

### Pagos únicos
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/payments` | Pago único CIT con tarjeta completa |
| `POST` | `/api/v1/payments/service` | Pago de servicio (factura) |
| `GET` | `/api/v1/payments/{id}` | Consultar pago |

### Tokens / DataVault
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/tokens` | Registrar tarjeta (sin cobrar) |
| `GET` | `/api/v1/tokens/{customer_id}` | Listar tarjetas de un cliente |
| `DELETE` | `/api/v1/tokens/{token}?customer_id=...` | Eliminar tarjeta |

### Clubs
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/clubs/{club_id}/pay` | Cobro on-demand CIT con token |

### Suscripciones recurrentes
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/recurring` | Crear suscripción (CIT STANDING_ORDER + DataVault) |
| `GET` | `/api/v1/recurring?customer_id=...` | Listar suscripciones de un cliente |
| `GET` | `/api/v1/recurring/{id}` | Ver detalle de suscripción |
| `POST` | `/api/v1/recurring/{id}/charge` | Cobrar manualmente (MIT STANDING_ORDER) |
| `POST` | `/api/v1/recurring/{id}/pause` | Pausar suscripción |
| `POST` | `/api/v1/recurring/{id}/resume` | Reanudar suscripción pausada |
| `DELETE` | `/api/v1/recurring/{id}` | Cancelar + DataVault DELETE |
| `POST` | `/api/v1/recurring/{id}/consent` | Registrar consentimiento (Visa/MC) |
| `GET` | `/api/v1/recurring/{id}/consent` | Ver registro de consentimiento |
| `GET` | `/api/v1/recurring/{id}/history` | Historial de cobros de la suscripción |

### Sistema
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/test/smoke` | Smoke test vs Azul sandbox |
| `GET` | `/test/scheduler/run` | Disparar scheduler manualmente (sandbox) |
| `GET` | `/docs` | Swagger UI |

