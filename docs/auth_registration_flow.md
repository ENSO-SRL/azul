# Flujo de Confirmación de Registro

Documenta el endpoint de envío de correo de confirmación y el JWT de información de usuario (`user_info`) generado al registrarse.

---

## Endpoint: Enviar correo de confirmación

**POST** `/auth/send_confirmation_email`

Envía el correo de confirmación de cuenta al usuario. Solo procede si el usuario existe, no ha sido confirmado aún, y tiene una suscripción activa distinta de "Gratis".

### Request body

```json
{
  "email": "usuario@ejemplo.com"
}
```

| Campo   | Tipo   | Requerido | Descripción           |
|---------|--------|-----------|-----------------------|
| `email` | string | Sí        | Correo del usuario    |

### Respuestas

**200 — Correo enviado**
```json
{
  "message": "Correo de confirmación enviado. Por favor revisa tu bandeja de entrada (y carpeta de spam)."
}
```

**200 — Correo no encontrado o cuenta ya confirmada** *(respuesta neutral para evitar enumeración de usuarios)*
```json
{
  "message": "Si el correo es válido y la cuenta no está confirmada, hemos enviado un mensaje de confirmación."
}
```

**403 — Sin suscripción premium**
```json
{
  "message": "Se requiere una suscripción premium para activar la cuenta."
}
```

**500 — Error interno**
```json
{
  "message": "Database error"
}
```

### Lógica interna

1. Busca el usuario por email (`is_active = true`).
2. Si no existe o ya está confirmado → respuesta 200 neutral (no revela si el correo existe).
3. Verifica que el usuario tenga al menos una `UserSubscription` con `activo = true`, `estado = "ACTIVA"` y `Subscription.name != "Gratis"`.
4. Si no tiene premium → `403`.
5. Genera un JWT de confirmación, lo registra en Redis como `confirmLatest:{email}` (invalida tokens anteriores) y envía el correo vía SendGrid.

---

## Cookie `user_info` (JWT)

Generada automáticamente al completar el registro (`POST /auth/sign_up`). Se setea como cookie HttpOnly con duración de 30 días.

### Cuándo se genera

- En `POST /auth/sign_up`, si el registro es exitoso (usuario nuevo creado).
- No se genera para el caso de re-registro de cuenta no confirmada.

### Configuración de la cookie

| Propiedad    | Valor                              |
|--------------|------------------------------------|
| Nombre       | `user_info`                        |
| HttpOnly     | `true`                             |
| Duración     | 30 días                            |
| Secure       | Según variable `COOKIE_SECURE`     |
| SameSite     | Según variable `COOKIE_SAMESITE`   |
| Domain       | Según variable `COOKIE_DOMAIN`     |

### Payload del JWT

```json
{
  "sub": "42",
  "email": "usuario@ejemplo.com",
  "name": "Juan",
  "last_name": "Pérez",
  "scope": "user_info",
  "exp": 1753000000
}
```

| Campo       | Tipo   | Descripción                              |
|-------------|--------|------------------------------------------|
| `sub`       | string | ID del usuario (como string)             |
| `email`     | string | Correo electrónico del usuario           |
| `name`      | string | Nombre del usuario                       |
| `last_name` | string | Apellido del usuario                     |
| `scope`     | string | Siempre `"user_info"`                    |
| `exp`       | int    | Unix timestamp de expiración (30 días)   |

### Cómo leerlo desde la API

```python
from utils.token_utils import decode_user_info_token

token = request.cookies.get("user_info")
user_data = decode_user_info_token(token)
# user_data = {"sub": "42", "email": "...", "name": "...", "last_name": "...", "scope": "user_info", "exp": ...}
# user_data es None si el token es inválido, expirado o tiene scope incorrecto
```

### Notas de seguridad

- El token usa la misma `SECRET_KEY` y algoritmo que el resto de tokens de la plataforma.
- Al ser HttpOnly, JavaScript del navegador no puede leerlo directamente; solo el servidor lo recibe en cada request.
- El scope `"user_info"` diferencia este token del access token (`"access_token"`) y del refresh token (`"refresh_token"`). `decode_user_info_token` rechaza cualquier token con scope diferente.
- No contiene contraseña ni datos sensibles.
