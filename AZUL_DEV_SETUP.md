# Azul Dev Setup — Onboarding Guide

Guía de configuración para nuevos desarrolladores del módulo de pagos.

---

## Prerequisitos

1. **Certificados mTLS** — solicitar a `solucionesintegradas@bpd.com.do`:
   - `iamatlas.local.crt` → certificado público
   - `iamatlas-azul-dev.key` → clave privada
   - Colocar en `certs/` (ignorado por `.gitignore` — nunca commitear)

2. **Credenciales Auth1/Auth2** — solicitar a `solucionesintegradas@bpd.com.do`:
   - Auth1 y Auth2 para modo **splitit** (ventas sin 3DS)
   - Auth1 y Auth2 para modo **3dsecure** (ventas con 3DS 2.0)

3. **Python 3.11+** con el launcher `py`:
   ```powershell
   py -3 --version
   ```

4. **Dependencias**:
   ```powershell
   py -3 -m pip install -r requirements.txt
   ```

---

## Configuración local

Copia `.env.example` → `.env` y rellena los valores:

```bash
# Modo desarrollo — lee certificados de disco en vez de AWS
AZUL_LOCAL_MODE=1

# Merchant ID (del contrato con BPD)
AZUL_MERCHANT_ID=39038540035

# Credenciales de autenticación (proporcionadas por Azul BPD)
AZUL_AUTH1_SPLITIT=<auth1 splitit>
AZUL_AUTH2_SPLITIT=<auth2 splitit>
AZUL_AUTH1_3DS=<auth1 3dsecure>
AZUL_AUTH2_3DS=<auth2 3dsecure>

# Rutas absolutas a los certificados mTLS
AZUL_CERT_PATH=C:\Users\<tu_usuario>\Desktop\azul_pagos_atlas\certs\iamatlas.local.crt
AZUL_KEY_PATH=C:\Users\<tu_usuario>\Desktop\azul_pagos_atlas\certs\iamatlas-azul-dev.key

# Entorno: sandbox (default) o production
AZUL_ENV=sandbox
```

---

## Smoke test

Valida mTLS + autenticación sin levantar el servidor:

```powershell
cd azul_pagos_atlas
py -3 smoke_test.py
```

Resultado esperado:
```
[*] Connecting to Azul sandbox (splitit mode)...
{
  "AuthorizationCode": "OK5054",
  "IsoCode": "00",
  "ResponseMessage": "APROBADA",
  ...
}
[OK] Smoke test PASSED -- IsoCode 00 (Approved)
```

---

## Levantar el servidor

```powershell
py -3 -m uvicorn app.main:app --reload --port 8000
```

- **Swagger UI**: http://localhost:8000/docs
- **Health check**: http://localhost:8000/health

---

## Checklist para ir a producción

### Credentials (BPD)
- [ ] Auth1/Auth2 de producción recibidos y cargados en AWS Secrets Manager
- [ ] Certificado de producción (distinto del de desarrollo) en AWS Secrets Manager
- [ ] Merchant ID de producción confirmado con BPD

### Infra
- [ ] `AZUL_LOCAL_MODE` removido (o puesto en `0`) en producción
- [ ] `AZUL_ENV=production` configurado en producción
- [ ] AWS Secrets Manager secrets creados:
  - `iamatlas/azul/prod/api-credentials`
  - `iamatlas/azul/prod/cert-pem`
  - `iamatlas/azul/prod/cert-key`

### Seguridad PCI
- [ ] Verificar que `Transaction.request_payload` en DB no contiene PAN completo
- [ ] Rotar credenciales Auth1/Auth2 si estuvieron en algún log
- [ ] Auditar accesos al Secrets Manager

### Testing
- [ ] Smoke test pasando en sandbox ✅
- [ ] Tarjeta aprobada en sandbox ✅
- [ ] Tarjeta declinada retorna `DECLINED` (no excepción) ✅
- [ ] Token CREATE/DELETE funciona en sandbox
- [ ] Cobro CIT con token funciona en sandbox
- [ ] Scheduler MIT dispara en sandbox
- [ ] Idempotency-Key — segundo intento devuelve mismo payment_id

### 3DS 2.0 (Fase 3)
- [ ] Activar 3DS en el merchant — solicitar a `solucionesintegradas@bpd.com.do`
- [ ] Solicitar tarjetas de prueba 3DS por escenario (Yes/No/Attempt/Rejected/Unavailable)

---

## Contacto BPD

**Email**: `solucionesintegradas@bpd.com.do`
**Referente comercial**: Luis Recio (Azul e-Commerce Solutions)

Usar este contacto para:
- Obtener/renovar Auth1/Auth2 de producción
- Obtener certificados mTLS de producción
- Activar 3DS 2.0 en el merchant
- Solicitar tarjetas de prueba específicas para 3DS
- Solicitar activación de Payment Page hospedado (baja alcance PCI a SAQ A)
- Consultar sobre TrxType adicionales (Hold, Post, Void, Refund)
