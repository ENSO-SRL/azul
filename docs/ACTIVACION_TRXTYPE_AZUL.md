# Activación TrxType CREATE / DELETE — Azul DataVault

> **Estado:** ⏳ Pendiente de activación por AZUL  
> **Fecha:** 2026-08-07  
> **Contacto:** Luis Recio (lrecio@azul.com.do)

---

## Problema

Al intentar tokenizar una tarjeta en DataVault **sin cobrar** (`TrxType=CREATE`), Azul responde:

```
VALIDATION_ERROR:TrxType
```

Esto significa que el merchant **no tiene habilitada** la operación `CREATE` ni `DELETE`.  
Solo `Sale` está activo.

### ¿Qué afecta?

| Flujo | Estado | Impacto |
|-------|--------|---------|
| Guardar tarjeta sin cobrar (onboarding) | ❌ Falla | No se puede registrar tarjeta para trial de 7 días |
| Eliminar tarjeta de DataVault (GDPR) | ❌ Falla | No se puede borrar token cuando usuario cancela |
| Cobro + guardar tarjeta (`Sale + SaveToDataVault=1`) | ✅ Funciona | Workaround temporal — cobra y tokeniza en un paso |

### Código afectado

- `app/infrastructure/azul_gateway.py` → método `create_token()` (línea ~507) envía `TrxType: "CREATE"`
- `app/infrastructure/azul_gateway.py` → método `delete_token()` (línea ~552) envía `TrxType: "DELETE"`
- `routers/checkout.py` → flujo de usuario nuevo (línea ~1437) llama `register_card()`

---

## Merchants que requieren activación

| Ambiente | Merchant ID | Nombre |
|----------|------------|--------|
| Sandbox | `39038540035` | Atlas (dev) |
| **Producción** | **`39644300001`** | **Colina del Sol** |

---

## Qué pedirle a Luis Recio

> "Luis, necesito que me activen dos operaciones en mis merchants:
>
> **Merchant sandbox:** `39038540035`  
> **Merchant producción:** `39644300001`
>
> 1. **`TrxType = CREATE`** — para tokenizar tarjetas en DataVault sin hacer cobro
> 2. **`TrxType = DELETE`** — para eliminar tokens del DataVault
>
> Ambas operaciones devuelven `VALIDATION_ERROR:TrxType`. Solo tengo `Sale` habilitado."

### Contacto

| | |
|---|---|
| **Nombre** | Luis Recio |
| **Email** | lrecio@azul.com.do |
| **CC** | jorodrigueza@azul.com.do, solucionesecommerce@azul.com.do |
| **Asunto sugerido** | Activar TrxType CREATE/DELETE — Merchants 39038540035 y 39644300001 |

### Operaciones adicionales pendientes (opcional)

Si quieres aprovechar y pedir todo de una vez:

| Operación | TrxType | Para qué sirve |
|-----------|---------|-----------------|
| Pre-autorización | `Hold` | Reservar monto sin cobrar (ej. hoteles) |
| Captura | `Post` | Cobrar una pre-autorización existente |
| Anulación | `Void` | Cancelar transacción del mismo día |
| Multi-moneda | `CurrencyPosCode=US$` | Cobrar en dólares |

---

## Workaround temporal

Mientras se activa `CREATE`, el checkout usa `Sale + SaveToDataVault=1` para todos los usuarios.  
Esto **cobra inmediatamente** y guarda la tarjeta en un solo paso (sin período de trial).

Buscar el comentario `WORKAROUND` en `routers/checkout.py` para restaurar el flujo original cuando Azul active `CREATE`.

---

## Variables de entorno — ECS / .env

### Azul Gateway (core)

| Variable | Descripción | Valor Producción | Valor Sandbox |
|----------|-------------|-----------------|---------------|
| `AZUL_ENV` | Ambiente activo | `production` | `sandbox` |
| `AZUL_MERCHANT_ID` | Merchant ID | `39644300001` | `39038540035` |
| `AZUL_AUTH1` | Auth header principal | `COLINADELSOL` | `splitit` |
| `AZUL_AUTH2` | Auth header secundario | *(secreto)* | `splitit` |
| `AZUL_AUTH1_SPLITIT` | Auth para modo splitit | `COLINADELSOL` | `splitit` |
| `AZUL_AUTH2_SPLITIT` | Auth para modo splitit | *(secreto)* | `splitit` |
| `AZUL_AUTH1_3DS` | Auth para 3D Secure | `COLINADELSOL` | `3dsecure` |
| `AZUL_AUTH2_3DS` | Auth para 3D Secure | *(secreto)* | `3dsecure` |
| `AZUL_LOCAL_MODE` | Usa archivos .env locales (no Secrets Manager) | `0` (ECS) | `1` (local) |

### Certificados mTLS

| Variable | Descripción | Notas |
|----------|-------------|-------|
| `AZUL_CERT_PATH` | Ruta al archivo .crt | Solo en local |
| `AZUL_KEY_PATH` | Ruta al archivo .key | Solo en local |
| `AZUL_CERT_PEM` | Contenido PEM del certificado (inline) | Usar en ECS/Docker |
| `AZUL_KEY_PEM` | Contenido PEM de la clave privada (inline) | Usar en ECS/Docker |

### AWS Secrets Manager (modo ECS)

| Variable | Descripción | Default |
|----------|-------------|---------|
| `AZUL_AWS_REGION` | Región de Secrets Manager | `us-east-2` |
| `AZUL_SECRET_CREDS` | Nombre del secret con Auth1/Auth2/MerchantId | `iamatlas/azul/prod/api-credentials` |
| `AZUL_SECRET_CERT` | Nombre del secret con cert PEM | `iamatlas/azul/prod/cert-pem` |
| `AZUL_SECRET_KEY` | Nombre del secret con key PEM | `iamatlas/azul/prod/cert-key` |

### Base de datos

| Variable | Descripción | Notas |
|----------|-------------|-------|
| `DATABASE_URL` | URL de conexión PostgreSQL (asyncpg) | RDS en us-east-2 |
| `DB_POOL_SIZE` | Tamaño del pool de conexiones | Default: `5` |
| `DB_MAX_OVERFLOW` | Conexiones adicionales permitidas | Default: `10` |
| `DB_POOL_TIMEOUT` | Timeout de conexión (segundos) | Default: `30` |
| `DB_SSL` | Modo SSL (`require` / `disable`) | Default: `require` |

### Seguridad / API

| Variable | Descripción | Notas |
|----------|-------------|-------|
| `API_KEY` | X-API-Key para autenticación de endpoints | Requerida en producción |
| `SECRET_KEY` | Clave compartida JWT (con api.iamatlas.do) | Para decodificar cookies |
| `ALGORITHM` | Algoritmo JWT | Default: `HS256` |

### URLs de la aplicación

| Variable | Descripción | Valor |
|----------|-------------|-------|
| `APP_BASE_URL` | URL pública de la app (para 3DS callbacks) | `https://pagos.iamatlas.do` (prod) |
| `AZUL_ECOMMERCE_URL` | URL del comercio (requerido por Azul) | Default: `https://atlas.do` |
| `AZUL_CUSTOMER_SERVICE_PHONE` | Teléfono de servicio al cliente | Requerido por doc Azul |
| `AZUL_ALT_MERCHANT_NAME` | Nombre alternativo del comercio | Opcional |
| `AUTH_API_BASE_URL` | URL del servicio de auth (Atlas) | `https://api.iamatlas.do` |

### URLs de Azul API

| Variable | Default |
|----------|---------|
| `AZUL_URL_SANDBOX` | `https://pruebas.azul.com.do/webservices/JSON/default.aspx` |
| `AZUL_URL_PRODUCTION` | `https://pagos.azul.com.do/WebServices/JSON/default.aspx` |
| `AZUL_URL_PRODUCTION_SECONDARY` | `https://contpagos.azul.com.do/Webservices/JSON/default.aspx` |
| `AZUL_3DS_METHOD_URL_SANDBOX` | `...?processthreedsmethod` |
| `AZUL_3DS_METHOD_URL_PROD` | `...?processthreedsmethod` |
| `AZUL_3DS_CHALLENGE_URL_SANDBOX` | `...?processthreedschallenge` |
| `AZUL_3DS_CHALLENGE_URL_PROD` | `...?processthreedschallenge` |
| `AZUL_VERIFY_PAYMENT_URL_SANDBOX` | `...?verifypayment` |
| `AZUL_VERIFY_PAYMENT_URL_PROD` | `...?verifypayment` |

### Notificaciones (AWS SES)

| Variable | Descripción | Default |
|----------|-------------|---------|
| `NOTIFY_FROM_EMAIL` | Dirección SES verificada (remitente) | Vacío = modo log |
| `NOTIFY_AWS_REGION` | Región de SES | `us-east-1` |
| `NOTIFY_ENABLED` | Habilitar/deshabilitar emails | Auto-detecta |

### Redis (Túnel)

| Variable | Descripción |
|----------|-------------|
| `REDIS_HOST` | Host de Redis Cloud |
| `REDIS_PORT` | Puerto |
| `REDIS_PASSWORD` | Contraseña |
| `REDIS_DB` | Base de datos (default: `0`) |
| `REDIS_SSL` | Usar SSL (`true`/`false`) |

### Otros

| Variable | Descripción |
|----------|-------------|
| `DEFAULT_CURRENCY` | Moneda por defecto (`DOP` o `USD`) |
| `DATA_RETENTION_DAYS` | Días de retención de pagos declinados (default: `90`) |
| `COMPANY_CARD_SECRET` | Clave Fernet para encriptar tarjeta de empresa |
| `ATLAS_TUNNEL_WEBHOOK_BASE_URL` | URL base del webhook del túnel |
| `ATLAS_TUNNEL_SECRET_KEY` | Clave secreta del webhook del túnel |

---

## Infraestructura ECS

| | Producción | QA |
|---|---|---|
| **Cluster** | `default` | `Atlas-Prueba` |
| **Service** | `pago-azul-svc` | `pago-azul-qa-svc` |
| **ECR Registry** | `293926505005.dkr.ecr.us-east-2.amazonaws.com` | Mismo |
| **ECR Repo** | `pago-azul` | `ambienteprueba-pago-azul` |
| **Deploy branch** | `main` | `qa` / `QA` |
| **IAM Role** | `GitHubActions-ECR-Deploy` (OIDC) | Static keys |
| **Región** | `us-east-2` (Ohio) | Mismo |
