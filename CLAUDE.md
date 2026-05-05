# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Atlas Pagos** is a FastAPI-based payment processing platform for Dominican businesses, integrated with **AZUL Payment Gateway** (BPD, República Dominicana). It handles one-time charges, recurring subscriptions (MIT/CIT), card tokenization via DataVault, 3DS 2.0 authentication, refunds, and automated reconciliation.

## Commands

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run the server (development)
```bash
uvicorn app.main:app --reload --port 8000
# Swagger UI: http://localhost:8000/docs
# Health: http://localhost:8000/health
```

### Run all tests
```bash
pytest tests/ -v
```

### Run a single test
```bash
pytest tests/test_gateway.py -v -k "approved"
pytest tests/test_recurring.py -v
```

### Smoke test (validates mTLS + auth against Azul sandbox, no server needed)
```bash
python smoke_test.py
```

### Docker
```bash
docker build -t pago-azul .
docker run -p 8000:8000 --env-file .env.prod pago-azul
```

## Architecture

The project follows **Clean Architecture** with three layers:

```
routers/          → HTTP layer (FastAPI routers, one file per domain area)
app/services/     → Business logic (PaymentService, RecurringService, etc.)
app/domain/       → Pure domain entities and repository interfaces (no I/O)
app/infrastructure/ → External adapters (Azul gateway, SQLAlchemy ORM, AWS SES)
```

**Request flow:** Router → Service → AzulGateway + Repository → DB/AZUL API

### Key components

- **`app/infrastructure/azul_gateway.py`** — The only code that calls the Azul API. Stateless; builds a fresh `httpx.AsyncClient` with mTLS per call. `_execute()` never raises on business declines (IsoCode ≠ 00) — a decline is a valid response. Only raises `AzulIntegrationError` when `ResponseCode="Error"` (our bug, not user's card). Implements production URL failover (primary → secondary) as required by Azul docs.

- **`app/infrastructure/azul_config.py`** — Config loader with `@lru_cache`. In local mode (`AZUL_LOCAL_MODE=1`) reads from `.env`; in production reads from three AWS Secrets Manager secrets. The two auth modes (`splitit` vs `3dsecure`) use different `Auth1`/`Auth2` header pairs.

- **`app/services/scheduler.py`** — APScheduler running inside the FastAPI process (`AsyncIOScheduler`). Fires MIT charges every hour, reminder emails daily at 09:00 UTC, reconciliation at 00:30 UTC. CustomOrderId is deterministic (`sha256(sub_id + attempt)[:20]`) to prevent duplicate charges on retries.

- **`app/domain/entities.py`** — Pure dataclasses with no I/O dependencies. Central enums: `IsoCode`, `AzulResponseCode`, `PaymentStatus`, `SubscriptionStatus`, `Currency`.

- **`app/infrastructure/database.py`** — Async SQLAlchemy engine. Uses SQLite by default (`azul_pagos.db`); set `DATABASE_URL` for PostgreSQL in production. Tables are auto-created on startup via `init_db()`.

### CIT/MIT indicator rules (Visa/Mastercard mandatory)

| Scenario | Indicator used |
|---|---|
| One-time charge with card | `cardholderInitiatedIndicator: "1"` |
| First charge of a subscription | `cardholderInitiatedIndicator: "STANDING_ORDER"` |
| Scheduler auto-charge (user absent) | `merchantInitiatedIndicator: "STANDING_ORDER"` |
| CIT on-demand with saved token | `cardholderInitiatedIndicator: "STANDING_ORDER"` |

### DataVault (tokenization)

Cards are stored in AZUL's DataVault, not locally. Atlas stores only the UUID token. When a subscription is cancelled, Atlas calls `TrxType=DELETE` to remove the token from the vault (GDPR compliance). `TrxType=CREATE` tokenizes without charging.

### 3DS 2.0 flow

Triggered by `auth_mode="3dsecure"` + `browser_info` in the payment request. The payment may pass through `PENDING_3DS_METHOD` (iframe) → `PENDING_3DS_CHALLENGE` (bank ACS redirect) before reaching `APPROVED`/`DECLINED`. Uses a separate set of auth credentials (`auth_3dsecure`).

### Idempotency

All charge endpoints accept `Idempotency-Key` header. A duplicate key returns the original payment without calling Azul again. The scheduler uses deterministic `CustomOrderId` values for the same guarantee.

## Environment Variables

```env
# Required
AZUL_LOCAL_MODE=1             # 1=use .env, 0=use AWS Secrets Manager
AZUL_MERCHANT_ID=...
AZUL_AUTH1_SPLITIT=...
AZUL_AUTH2_SPLITIT=...
AZUL_AUTH1_3DS=...
AZUL_AUTH2_3DS=...
AZUL_CERT_PATH=/path/to/cert.crt   # or AZUL_CERT_PEM for inline PEM
AZUL_KEY_PATH=/path/to/cert.key    # or AZUL_KEY_PEM for inline PEM
AZUL_ENV=sandbox              # sandbox | production

# Optional
DATABASE_URL=sqlite+aiosqlite:///./azul_pagos.db
NOTIFY_FROM_EMAIL=...         # AWS SES verified sender
NOTIFY_ENABLED=1
APP_BASE_URL=https://your-domain.com
DEFAULT_CURRENCY=DOP
```

mTLS certificates (`certs/`) are gitignored and must be requested from `solucionesintegradas@bpd.com.do`.

## PCI Compliance Notes

- `Transaction.request_payload` always stores a masked PAN (digits 7–15 replaced with `*`) via `_mask_sensitive()` in the gateway.
- CVC is fully masked in stored logs.
- Cards are never stored in Atlas's database — only DataVault tokens.
- The Dockerfile runs as a non-root user.

## Tests

- `tests/test_gateway.py` — Integration tests hitting the real Azul sandbox (requires `.env` + certs + running network). Uses the 10-card test whitelist for sandbox merchant `39038540035`.
- `tests/test_recurring.py` — Unit tests using `unittest.mock` (no real Azul calls).
- `tests/test_sandbox_integration.py` — End-to-end sandbox flows.
- `tests/test_scheduler.py` — Scheduler logic tests.
