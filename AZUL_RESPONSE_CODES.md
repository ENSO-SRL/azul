# Azul Response Codes — Reference

Guía de referencia para interpretar respuestas del Azul Payment Gateway.

## Flujo de interpretación

```
response = azul.post(payload)

if response["ResponseCode"] == "Error":
    # Bug tuyo: credenciales malas, payload inválido, merchant ID incorrecto.
    # NO es una declinada del banco emisor. No reintentar sin corregir el payload.
    handle_validation_error(response["ErrorDescription"])

elif response["ResponseCode"] == "ISO8583":
    iso = response["IsoCode"]
    if iso == "00":
        # Único caso de éxito
        mark_approved()
    else:
        # Declinada o challenge — ver tabla abajo
        handle_decline(iso)
```

---

## IsoCode — Tabla completa

| IsoCode | Nombre | Descripción | Acción recomendada |
|---------|--------|-------------|-------------------|
| `00` | **APROBADA** | Único resultado de éxito | Marcar pago como APPROVED |
| `3D` | **3DS_CHALLENGE** | El banco emisor requiere autenticación del tarjetahabiente | Iniciar flujo 3DS 2.0 (redirect al ACS) |
| `08` | **NOT_AUTHENTICATED** | ACS del emisor no disponible durante 3DS | Reintentar con `ForceNo3DS=1` o notificar al usuario |
| `51` | **DECLINED_FUNDS** | Fondos insuficientes, límite excedido, o tarjeta declinada genérica | No reintentar automáticamente — notificar al usuario |
| `63` | **SECURITY_VIOLATION** | Violación de seguridad detectada por el procesador | Bloquear transacción, notificar al equipo de seguridad |
| `99` | **ERROR_GENERIC** | Error genérico — incluye varios sub-casos (ver abajo) | Depende del `ErrorDescription` |
| `""` | **UNKNOWN** | Sin IsoCode — respuesta pre-procesador | Ver `ResponseCode=Error` |

### IsoCode 99 — Sub-casos conocidos

| ErrorDescription | Causa | Acción |
|-----------------|-------|--------|
| `ERROR CVC` | CVC incorrecto | Pedir al usuario que verifique el CVV |
| `PROVEEDOR INVALIDO` | Merchant ID inválido o deshabilitado | Contactar soporte BPD |
| `Tarjeta Invalida` | PAN inválido o tarjeta bloqueada | No reintentar |
| `Transaction declined. 3D Secure authentication failed.` | Falló autenticación 3DS | No reintentar automáticamente |

---

## ResponseCode — Nivel superior

| ResponseCode | Significado | ¿Quién lo genera? |
|-------------|-------------|-----------------|
| `ISO8583` | Respuesta válida del procesador — revisar `IsoCode` | Azul / banco emisor |
| `Error` | Error de validación antes del procesador — revisar `ErrorDescription` | Azul (validación de payload/auth) |

### ResponseCode=Error — Sub-casos conocidos

| ErrorDescription | Causa | Fix |
|-----------------|-------|-----|
| `MISSING_AUTH_HEADER:Auth1` | Header Auth1 ausente | Verificar `AZUL_AUTH1` en .env |
| `INVALID_AUTH:Auth1` | Credenciales incorrectas | Solicitar a solucionesintegradas@bpd.com.do |
| `VALIDATION_ERROR:Amount` | Amount vacío o formato inválido | Amount debe ser string de centavos enteros |
| `VALIDATION_ERROR:Itbis` | ITBIS inválido (e.g. "0" cuando se esperaba "000" o cantidad proporcional) | Usar `str(itbis).zfill(3)` o al menos "000" |
| `INVALID_MERCHANTID` | MerchantID no existe o no tiene acceso | Verificar `AZUL_MERCHANT_ID` |
| `Original transaction is invalid or has already been voided` | AzulOrderId de un Void/Post inválido | Verificar que la transacción original existe |

---

## Enums en código

```python
# app/domain/entities.py

class IsoCode(str, Enum):
    APPROVED           = "00"
    THREE_DS_CHALLENGE = "3D"
    NOT_AUTHENTICATED  = "08"
    DECLINED_FUNDS     = "51"
    SECURITY_VIOLATION = "63"
    ERROR_GENERIC      = "99"
    UNKNOWN            = ""

class AzulResponseCode(str, Enum):
    ISO8583 = "ISO8583"
    ERROR   = "Error"
    UNKNOWN = ""
```

---

## Lógica de status en PaymentService

```
APPROVED  → IsoCode == "00"
DECLINED  → ResponseCode == "ISO8583" AND IsoCode != "00"
ERROR     → ResponseCode == "Error" OR IsoCode == ""
```

Un pago DECLINED no es un bug — es una respuesta válida del sistema bancario.
Un pago ERROR generalmente indica un problema de integración (credentials, payload).
