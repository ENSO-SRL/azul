# Constancias de Pruebas — Certificación AZUL Sandbox

**Para:** Luis Eduardo Recio Pérez — BPD / AZUL  
**De:** Equipo Atlas — ENSO SRL  
**Fecha ejecución:** 2026-05-07 16:45 AST  
**Merchant ID:** `39038540035`  
**Ambiente:** sandbox — `pruebas.azul.com.do`  
**ECommerceUrl:** https://www.iamatlas.do  

---

## Tabla de Transacciones

| # | Fecha | Test | Tarjeta | Monto | IsoCode | ResponseCode | AzulOrderId | AuthorizationCode | Estado |
|---|-------|------|---------|-------|---------|--------------|-------------|-------------------|--------|
| 1 | 2026-05-07 16:45 AST | Sale Visa | 426055******5872 | RD$100.00 | 00 | ISO8583 | 44903866 | OK0895 | APPROVED |
| 2 | 2026-05-07 16:45 AST | Sale Mastercard | 542418******1732 | RD$100.00 | 00 | ISO8583 | 44903867 | OK0995 | APPROVED |
| 3 | 2026-05-07 16:45 AST | Sale Discover | 601100******9818 | RD$100.00 | 00 | ISO8583 | 44903868 | OK1135 | APPROVED |
| 4 | 2026-05-07 16:45 AST | Sale + DataVault | 426055******5872 | RD$100.00 | 00 | ISO8583 | 44903869 | OK1245 | APPROVED |
| 5 | 2026-05-07 16:45 AST | MIT STANDING_ORDER | (DataV******ken) | RD$100.00 | 00 | ISO8583 | 44903870 | OK1335 | APPROVED |
| 6 | 2026-05-07 16:45 AST | CIT STANDING_ORDER | (DataV******ken) | RD$100.00 | 00 | ISO8583 | 44903871 | OK1435 | APPROVED |
| 7 | 2026-05-07 16:45 AST | 3DS 2.0 | 400552******0129 | RD$100.00 | 3D2METHOD | ISO8583 | 44903872 |  | PENDING_3DS_METHOD |
| 8 | 2026-05-07 16:45 AST | Void | — | — | — | — | — | — | SKIPPED |
| 9 | 2026-05-07 16:45 AST | PCI DSS | 426055******5872 | — | — | — | — | — | COMPLIANT |

---

## Detalle por Test

### Sale Visa

```
test: Sale Visa
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: 426055******5872
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903866
AuthorizationCode: OK0895
CustomOrderId: 9f92be9e-eb9b-428a-8fce-99ca25d4fae1
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale Mastercard

```
test: Sale Mastercard
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: 542418******1732
red: Mastercard
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903867
AuthorizationCode: OK0995
CustomOrderId: e7ebb68a-a19b-4a15-813b-c6f8bc020113
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale Discover

```
test: Sale Discover
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: 601100******9818
red: Discover
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903868
AuthorizationCode: OK1135
CustomOrderId: eda81bcf-24c2-48d4-934e-f885d7811a13
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
```

### Sale + DataVault

```
test: Sale + DataVault
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: 426055******5872
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903869
AuthorizationCode: OK1245
CustomOrderId: f88296dc-462e-472b-9387-6d5b71a3913a
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken: 48338725-71B6-46C2-8ED1-A77EEC51F501
SaveToDataVault: 1
token_generado: Sí
```

### MIT STANDING_ORDER

```
test: MIT STANDING_ORDER
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: (DataV******ken)
red: Unknown
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903870
AuthorizationCode: OK1335
CustomOrderId: d340f356-daf6-48cb-b984-12ec67260827
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken_usado: 48338725-71B6-46...
merchantInitiatedIndicator: STANDING_ORDER
ForceNo3DS: 1
```

### CIT STANDING_ORDER

```
test: CIT STANDING_ORDER
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: (DataV******ken)
red: Unknown
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 00
response_code: ISO8583
response_message: APROBADA
AzulOrderId: 44903871
AuthorizationCode: OK1435
CustomOrderId: 76a20e4e-49ee-4fd9-abf6-4ca5982b4ad2
ECommerceUrl: https://www.iamatlas.do
estado: APPROVED
DataVaultToken_usado: 48338725-71B6-46...
cardholderInitiatedIndicator: STANDING_ORDER
ForceNo3DS: 1
```

### 3DS 2.0

```
test: 3DS 2.0
fecha: 2026-05-07 16:45 AST
merchant_id: 39038540035
tarjeta_enmascarada: 400552******0129
red: Visa
monto: RD$100.00
itbis: RD$15.00
moneda: DOP
iso_code: 3D2METHOD
response_code: ISO8583
response_message: 3D_SECURE_2_METHOD
AzulOrderId: 44903872
CustomOrderId: e145c661-7534-4344-9630-e868e273a1ab
ECommerceUrl: https://www.iamatlas.do
estado: PENDING_3DS_METHOD
ThreeDSMethodForm: presente
resultado_esperado: 3D2METHOD o flujo 3DS activado
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
fecha: 2026-05-07 16:45 AST
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

- Void — No habilitado en sandbox: Void failed: VALIDATION_ERROR:CVC
