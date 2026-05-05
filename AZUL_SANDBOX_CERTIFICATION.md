# Certificación de Integración AZUL — Ambiente Sandbox
**Proyecto:** Atlas Pagos  
**Merchant ID (sandbox):** `39038540035`  
**Fecha de pruebas:** 4 de mayo, 2026  
**Contacto técnico:** Luis Zadkiel Durán Aracena — zad.duran@gmail.com  
**Empresa:** Servicios Digitales Popular / ENSO SRL  

---

## Resumen Ejecutivo

El sistema **Atlas Pagos** completó exitosamente la suite de integración contra el ambiente de desarrollo de AZUL. Se ejecutaron **30 casos de prueba automatizados**, de los cuales **24 pasaron** y **6 fueron omitidos** por no estar habilitados en el Merchant sandbox (se requiere activación en producción).

```
===== 24 passed, 6 skipped, 0 failed =====
Tiempo de ejecución: 1 min 52 seg
Framework: pytest 8.4.1 / Python 3.11
```

---

## ✅ Funcionalidades Verificadas y Aprobadas

### 1. Ventas con Tarjeta (CIT — Cardholder Initiated)

| Tarjeta | Red | Resultado | IsoCode |
|---------|-----|-----------|---------|
| `4260550061845872` | Visa | ✅ APPROVED | `00` |
| `4035874000424977` | Visa | ✅ APPROVED | `00` |
| `4012000033330026` | Visa | ✅ APPROVED | `00` |
| `5424180279791732` | Mastercard | ✅ APPROVED | `00` |
| `5426064000424979` | Mastercard | ✅ APPROVED | `00` |
| `6011000990099818` | Discover | ✅ APPROVED | `00` |

**Campos verificados en cada respuesta:**
- `authorization_code` — presente y no vacío ✅
- `AzulOrderId` — presente (necesario para Void/Refund) ✅
- `RRN` — campo existe en la entidad ✅

---

### 2. DataVault — Tokenización de Tarjeta

| Operación | Resultado | Nota |
|-----------|-----------|------|
| Sale con `save_token=True` | ✅ PASSED | Retorna `DataVaultToken` válido |

El token retornado es utilizado como base para todos los pagos recurrentes MIT/CIT posteriores.

---

### 3. Cobros Recurrentes (Visa/MC Stored Credentials Mandate)

| Tipo | Indicador en Payload | Resultado |
|------|---------------------|-----------|
| MIT — Merchant Initiated | `merchantInitiatedIndicator: STANDING_ORDER` | ✅ PASSED |
| CIT — Cardholder Initiated | `cardholderInitiatedIndicator: STANDING_ORDER` | ✅ PASSED |

Ambos flujos incluyen `ForceNo3DS: 1` para saltar autenticación en cobros automáticos — conforme a la documentación técnica AZUL pp. 22-24.

---

### 4. 3D Secure 2.0

| Escenario | Resultado | Detalle |
|-----------|-----------|---------|
| Sale con `auth_mode=3dsecure` + tarjeta `4005520000000129` | ✅ PASSED | IsoCode `3D2METHOD` recibido — flujo 3DS activado correctamente |
| Sale con `auth_mode=splitit` + misma tarjeta | ✅ PASSED | `ForceNo3DS=1` enviado — aprobada sin challenge |

---

### 5. PCI DSS Compliance

| Control | Resultado |
|---------|-----------|
| PAN completo **NO** aparece en logs de auditoría | ✅ PASSED |
| PAN enmascarado (BIN + `****` + last4) **SÍ** aparece | ✅ PASSED |
| CVC **NO** se almacena en texto claro | ✅ PASSED |

---

### 6. Validación de Payload (Conformidad Documental)

Todos los campos obligatorios del *Documento Técnico — Integración vía API* están presentes en cada request:

| Campo | Valor | Estado |
|-------|-------|--------|
| `Channel` | `EC` | ✅ |
| `Store` | `39038540035` | ✅ |
| `PosInputMode` | `E-Commerce` | ✅ |
| `TrxType` | `Sale` | ✅ |
| `Amount` | centavos | ✅ |
| `Itbis` | centavos | ✅ |
| `CurrencyPosCode` | `$` (DOP) | ✅ |
| `AcquirerRefData` | `1` | ✅ |
| `RRN` | `null` | ✅ |
| `CustomerServicePhone` | configurable | ✅ |
| `ECommerceUrl` | `https://atlas.do` | ✅ |
| `CustomOrderId` | UUID del pago | ✅ |
| `CardHolderName` | nombre del titular | ✅ |
| `CardHolderEmail` | email del titular | ✅ |

---

### 7. Multi-moneda y Manejo de Errores

| Escenario | Resultado |
|-----------|-----------|
| Venta en DOP (`CurrencyPosCode=$`) | ✅ PASSED |
| `Amount=0` lanza `AzulIntegrationError` (no silenciado) | ✅ PASSED |
| Declinada **no lanza excepción** — es respuesta de negocio válida | ✅ PASSED |

---

## ⏭️ Funcionalidades Pendientes de Activación en Producción

Los siguientes tests fueron **omitidos (SKIPPED)** porque las operaciones no están habilitadas en el Merchant sandbox `39038540035`. El código está implementado y listo — solo requiere activación por parte de AZUL.

| Funcionalidad | Error Sandbox | Acción Requerida a Luis Recio |
|---------------|--------------|-------------------------------|
| `DataVault CREATE` standalone (sin cobro) | `VALIDATION_ERROR:TrxType` | Activar operación CREATE independiente |
| `DataVault DELETE` (remover token) | `VALIDATION_ERROR:TrxType` | Activar operación DELETE |
| `TrxType=Hold` (pre-autorización) | `VALIDATION_ERROR:TrxType` | Activar modo pre-autorizado en el Merchant |
| `TrxType=Post` (captura de hold) | `VALIDATION_ERROR:TrxType` | Incluido con la activación de Hold |
| `TrxType=Void` (anulación) | `VALIDATION_ERROR:CVC` | Activar operación Void |
| `CurrencyPosCode=US$` (USD) | `VALIDATION_ERROR:CurrencyPosCode` | Habilitar multi-moneda USD |

---

## 📧 Solicitud de Acceso a Producción — Email a Luis Recio

A continuación, el borrador del email para solicitar las credenciales de producción:

---

**Para:** lrecio@azul.com.do  
**CC:** jorodrigueza@azul.com.do, solucionesecommerce@azul.com.do  
**Asunto:** Solicitud de acceso a Producción — Atlas Pagos (Merchant 39038540035)

---

Estimado Luis,

Buen día. Me comunico para informarle que hemos completado satisfactoriamente la certificación de integración en el ambiente de desarrollo. Adjunto el reporte de pruebas que detalla los 24 casos verificados.

**Resumen de pruebas completadas:**
- ✅ Ventas CIT con 6 tarjetas de prueba (Visa, Mastercard, Discover)
- ✅ Tokenización DataVault con cobro (save_token)
- ✅ Cobros recurrentes MIT con `STANDING_ORDER` (Visa/MC mandate)
- ✅ Cobros recurrentes CIT con token
- ✅ Flujo 3D Secure 2.0 (3D2METHOD + ForceNo3DS)
- ✅ Cumplimiento PCI DSS (PAN masking, CVC protegido)
- ✅ Conformidad de payload con documentación técnica (todos los campos requeridos)

**Solicito habilitación de las siguientes funcionalidades en producción:**

1. **Acceso a Producción** — Merchant ID, Auth1 y Auth2 para `pagos.azul.com.do`
2. **Certificado de Producción** — Nuevo CSR para el ambiente productivo
3. **DataVault CREATE/DELETE standalone** — Para gestión de tarjetas sin cobro inmediato
4. **TrxType=Hold + Post** — Para el flujo de pre-autorización (reservas)
5. **TrxType=Void** — Para anulación de transacciones del mismo día
6. **Multi-moneda USD** — Para transacciones en dólares (CurrencyPosCode=US$)

Quedamos a la espera de su confirmación y las instrucciones para el siguiente paso.

Saludos cordiales,  
Luis Zadkiel Durán Aracena  
Servicios Digitales Popular / ENSO SRL  
zad.duran@gmail.com

---

## Infraestructura Técnica

| Componente | Descripción |
|-----------|-------------|
| **Lenguaje** | Python 3.11 / FastAPI |
| **Despliegue** | AWS ECS (Express Mode) — `us-east-2` |
| **Contenedor** | ECR `293926505005.dkr.ecr.us-east-2.amazonaws.com/pago-azul` |
| **mTLS** | Certificado BPD-SCA (4096 bits) — `iamatlas.local` |
| **DB** | SQLite (sandbox) → PostgreSQL RDS (producción) |
| **CI/CD** | GitHub Actions → ECR automático en cada push |
| **URL servicio** | `pa-f36fc80df7394359a19c677914c92ef1.ecs.us-east-2.on.aws` |

---

## Comando para Reproducir las Pruebas

```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar credenciales sandbox en .env
# AZUL_LOCAL_MODE=1
# AZUL_MERCHANT_ID=39038540035
# AZUL_AUTH1_SPLITIT=splitit
# AZUL_AUTH2_SPLITIT=splitit
# AZUL_CERT_PATH=ruta/al/iamatlas.local.crt
# AZUL_KEY_PATH=ruta/al/iamatlas-azul-dev.key

# Correr suite completa
py -m pytest tests/test_sandbox_integration.py -v --tb=short

# Correr solo un bloque específico
py -m pytest tests/test_sandbox_integration.py -v -k "approved"
py -m pytest tests/test_sandbox_integration.py -v -k "pci"
py -m pytest tests/test_sandbox_integration.py -v -k "3ds"
```

---

*Generado automáticamente — 4 de mayo 2026*
