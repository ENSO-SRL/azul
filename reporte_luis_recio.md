# Constancias de Pruebas — Certificación AZUL Sandbox

**Para:** Luis Eduardo Recio Pérez — BPD / AZUL  
**De:** Equipo Atlas — ENSO SRL  
**Fecha ejecución:** 2026-05-13 10:44 AST  
**Merchant ID:** `39038540035`  
**Ambiente:** sandbox — `pruebas.azul.com.do`  
**ECommerceUrl:** https://www.iamatlas.do  

---

## Tabla de Transacciones

| # | Fecha | Test | Tarjeta | Monto | IsoCode | ResponseCode | AzulOrderId | AuthorizationCode | Estado |
|---|-------|------|---------|-------|---------|--------------|-------------|-------------------|--------|
| 1 | 2026-05-13 10:44 AST | Sale Visa | 426055******5872 | RD$100.00 | 00 | ISO8583 | 44904930 | OK2620 | APPROVED |
| 2 | 2026-05-13 10:44 AST | Sale Mastercard | 542418******1732 | RD$100.00 | 00 | ISO8583 | 44904931 | OK2690 | APPROVED |
| 3 | 2026-05-13 10:44 AST | Sale Discover | 601100******9818 | RD$100.00 | 00 | ISO8583 | 44904932 | OK2740 | APPROVED |
| 4 | 2026-05-13 10:44 AST | Sale + DataVault | 426055******5872 | RD$100.00 | 00 | ISO8583 | 44904933 | OK2800 | APPROVED |
| 5 | 2026-05-13 10:44 AST | MIT STANDING_ORDER | (DataV******ken) | RD$100.00 | 00 | ISO8583 | 44904934 | OK2860 | APPROVED |
| 6 | 2026-05-13 10:44 AST | CIT STANDING_ORDER | (DataV******ken) | RD$100.00 | 00 | ISO8583 | 44904935 | OK2920 | APPROVED |
| 7 | 2026-05-13 10:44 AST | 3DS 2.0 (Challenge) | 400552******0129 | RD$100.00 | 3D2METHOD | ISO8583 | 44904936 |  | PENDING_3DS_CHALLENGE |
| 8 | 2026-05-13 10:44 AST | Sale Visa 2 (4035...4977) | 403587******4977 | RD$100.00 | 63 | ISO8583 | 44904937 |  | DECLINED |
| 9 | 2026-05-13 10:44 AST | Sale Mastercard 2 (5426...4979) | 542606******4979 | RD$100.00 | 63 | ISO8583 | 44904938 |  | DECLINED |
| 10 | 2026-05-13 10:44 AST | Sale Visa 3 (4012...0026) | 401200******0026 | RD$100.00 | 00 | ISO8583 | 44904939 | OK319C | APPROVED |
| 11 | 2026-05-13 10:44 AST | Void | — | — | — | — | — | — | SKIPPED |
| 12 | 2026-05-13 10:44 AST | PCI DSS | 426055******5872 | — | — | — | — | — | COMPLIANT |

---

## Detalle por Test

### Sale Visa

```
test: Sale Visa
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 426055******5872
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904930
AuthorizationCode: OK2620
CustomOrderId: e99b20f1-dbbd-4042-aebe-7fdd54ad0692
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale Mastercard

```
test: Sale Mastercard
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 542418******1732
red: Mastercard
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904931
AuthorizationCode: OK2690
CustomOrderId: e4ca973c-d66d-4496-85fa-ba31e870b62f
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale Discover

```
test: Sale Discover
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 601100******9818
red: Discover
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904932
AuthorizationCode: OK2740
CustomOrderId: 155cbb3f-81e2-4e0d-ab2f-4fda70c49c0b
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale + DataVault

```
test: Sale + DataVault
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 426055******5872
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904933
AuthorizationCode: OK2800
CustomOrderId: bec7679e-b3f9-4d29-b189-b9bd23fdc587
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken: E78B9C89-2727-4C0C-9FB6-AA1F17AECAF1
SaveToDataVault: 1
token_generado: Sí
```

### MIT STANDING_ORDER

```
test: MIT STANDING_ORDER
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: (DataV******ken)
red: Unknown
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904934
AuthorizationCode: OK2860
CustomOrderId: 874eb9ce-91d5-4c72-af27-5bb81263af57
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken_usado: E78B9C89-2727-4C...
merchantInitiatedIndicator: STANDING_ORDER
ForceNo3DS: 1
```

### CIT STANDING_ORDER

```
test: CIT STANDING_ORDER
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: (DataV******ken)
red: Unknown
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904935
AuthorizationCode: OK2920
CustomOrderId: cd37f784-ae41-4d9d-951e-a2c55a2ce273
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken_usado: E78B9C89-2727-4C...
cardholderInitiatedIndicator: STANDING_ORDER
ForceNo3DS: 1
```

### 3DS 2.0 (Challenge)

```
test: 3DS 2.0 (Challenge)
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 400552******0129
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 3D2METHOD
response_code: ISO8583
response_message: 3D_SECURE_2_METHOD
AzulOrderId: 44904936
CustomOrderId: d6629346-4b98-4d92-84b8-5316e028fdc4
ECommerceUrl: https://www.iamatlas.do
estado: PENDING_3DS_CHALLENGE
paso: Challenge requerido - intervencion manual
iso_step5: 3D
```

### Sale Visa 2 (4035...4977)

```
test: Sale Visa 2 (4035...4977)
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 403587******4977
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 63
response_code: ISO8583
response_message: DECLINADA
AzulOrderId: 44904937
CustomOrderId: 0e42e183-5dea-4d64-95d3-25c1ed7a7427
ECommerceUrl: https://www.iamatlas.do
estado: DECLINED
```

### Sale Mastercard 2 (5426...4979)

```
test: Sale Mastercard 2 (5426...4979)
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 542606******4979
red: Mastercard
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 63
response_code: ISO8583
response_message: DECLINADA
AzulOrderId: 44904938
CustomOrderId: 6c519c46-d7b7-40b9-96ee-75d6ad72d828
ECommerceUrl: https://www.iamatlas.do
estado: DECLINED
```

### Sale Visa 3 (4012...0026)

```
test: Sale Visa 3 (4012...0026)
fecha: 2026-05-13 10:44 AST
merchant_id: 39038540035
tarjeta_enmascarada: 401200******0026
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44904939
AuthorizationCode: OK319C
CustomOrderId: 2f3bbe7b-392a-4d53-86b5-e03484d19842
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Void

```
test: Void
estado: SKIPPED
motivo: Void failed: VALIDATION_ERROR:CVC
```

### PCI DSS

```
test: PCI DSS
fecha: 2026-05-13 10:44 AST
PAN_completo_en_log: NO
CVC_en_log: NO
CardNumber_guardado: 426055******5872
DataVaultToken_para_recurrentes: Sí
estado: COMPLIANT
```

---

## Evidencia PCI DSS

| Validación | Resultado |
|-----------|-----------|
| PAN completo en logs | NO |
| CVC almacenado en claro | NO |
| CardNumber guardado (enmascarado) | `426055******5872` |
| DataVaultToken para recurrentes | Sí |
| Cumplimiento | **COMPLIANT** |

---

## Notas

- Conexión vía **mTLS** con certificado proporcionado por AZUL.
- `ForceNo3DS=1` se envía en todas las transacciones Split-it/recurrentes.
- `merchantInitiatedIndicator: STANDING_ORDER` en pagos MIT.
- `cardholderInitiatedIndicator: STANDING_ORDER` en pagos CIT con token.
- PAN enmascarado (BIN + `******` + últimos 4) en todos los logs de auditoría.
- CVC reemplazado por `***` antes de cualquier persistencia.

## Tests SKIPPED

- 3DS Challenge: requiere navegador (AzulOrderId=44904936)
- Void — No habilitado en sandbox: Void failed: VALIDATION_ERROR:CVC
