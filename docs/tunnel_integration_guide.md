# Guía de Integración del Túnel de Pagos (Para el Frontend)

Esta guía detalla el flujo que debe seguir la página de pagos de Atlas (Túnel) para recuperar el resumen de cobro y enviar de forma segura los datos de la tarjeta a nuestra API.

## 1. El Link de Pago y el Resumen en Redis

Cuando el usuario hace clic en el botón de pagar, será redirigido a una URL que tú compones. Esa URL contendrá un parámetro (por ejemplo, `ref`) que representa el ID único de la transacción.

**Ejemplo de URL generada:**
`https://pago.atlas.do/tunnel?ref=c8e7a6b2-4d3f-4f2a-b1c2-d3e4f5a6b7c8`

Con ese `ref` (el ID de la reserva), puedes buscar el resumen del pago conectándote a nuestro Redis.

* **Clave de Redis:** `atlas_card_transaction:{ref}`
* **Formato:** String (JSON)
* **Contenido de ejemplo:**
  ```json
  {
      "service_name": "Reserva - Santo Domingo Country Club",
      "date": "2026-07-15",
      "total": "$150.00",
      "taxes": "$10.00",
      "technology_fee": "$2.50"
  }
  ```
*(Nota: campos como `taxes` o `technology_fee` pueden no venir incluidos si el club no los cobra).*

Una vez leído, puedes renderizar esta información en la pantalla de pago para el usuario.

---

## 2. Envío de Datos por el Webhook

Cuando el usuario coloque la información de su tarjeta de crédito y presione pagar, tu frontend (o tu backend intermediario del frontend) deberá enviar una confirmación asíncrona hacia el servicio.

* **Método:** `POST`
* **Endpoint:** `https://tu-dominio-backend.com/payments/tunnel-webhook/{ref}`
* **Content-Type:** `application/json`

### a. Seguridad: Cifrado de la Tarjeta (Fernet)

Por seguridad, la tarjeta y el CVC jamás viajan en texto plano. Deben ser encriptados utilizando **Fernet (AES-128-CBC)**.

Para la llave de cifrado, usamos el SHA-256 de nuestra clave compartida secreta (`ATLAS_TUNNEL_SECRET_KEY`), codificada en Base64 URL-Safe:
```python
# Ejemplo en Python de cómo encriptarías la tarjeta
import hashlib
import base64
from cryptography.fernet import Fernet

SECRET_KEY = "tu_secreto_compartido_aqui"
# Derivar la llave Fernet
fernet_key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())
cipher = Fernet(fernet_key)

card_enc = cipher.encrypt(b"4111222233334444").decode()
cvc_enc = cipher.encrypt(b"123").decode()
```

### b. Payload JSON
El cuerpo de la petición debe lucir de la siguiente manera:
```json
{
    "card_number_enc": "gAAAAAB... (texto encriptado)",
    "cvc_enc": "gAAAAAB... (texto encriptado)",
    "expiration": "202812",
    "cardholder_name": "Juan Perez",
    "postal_code": "10131"
}
```
*(Nota: `expiration` formato YYYYMM)*

### c. Seguridad: Firma de la Petición (HMAC)

Para validar que la petición viene de ti, el endpoint requiere un header de firma:
* **Header:** `X-Atlas-Signature`

Este header contiene la firma `HMAC-SHA256` en formato hexadecimal calculada sobre el **raw body de la petición** (los bytes crudos del JSON) utilizando el `ATLAS_TUNNEL_SECRET_KEY` como llave.

```python
# Ejemplo en Python de la firma
import hmac
import hashlib

raw_body = b'{"card_number_enc": "...", ...}'
signature = hmac.new(SECRET_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
# signature = "e4d909c290d0fb1ca068ffaddf22cbd0..."
```

---

## 3. Fin del Flujo

Si el webhook responde `200 OK`, nuestro servicio desencriptará exitosamente la tarjeta, desbloqueará la automatización que estaba pausada y procederá a completar la reserva en el club. En tu frontend, en ese momento, puedes mostrarle al usuario una pantalla de "Pago en proceso, confirmando reserva...".
