# Checkout: migrar identificación de usuario de `access_token` a `user_info`

## Problema

El checkout identifica al usuario logueado leyendo la cookie **`access_token`**
y decodificándola con `decode_user_info_token()`, que espera el payload
`{sub, email, name, last_name, scope: "user_info"}`.

Ese shape **nunca coincidió** con el `access_token` real que emite el servicio
de auth (`api.iamatlas.do`): `access_token` real trae `scope: "access_token"`,
`sub` = email (no user_id), y no trae `name`/`last_name`.

Con un cambio reciente del lado de auth, además, `access_token` **deja de
existir directamente** para usuarios sin tarjeta ni suscripción activa
(`sign_in`/`google_sign_in` responden 403 y no lo setean). Esos usuarios nunca
pueden ser identificados en checkout para ofrecerles agregar una tarjeta —
candado circular.

Auth ya expone del lado de ellos una cookie separada, **`user_info`**, pensada
para este caso: mismo `SECRET_KEY`/`ALGORITHM` (HS256), payload
`{sub: user_id, email, name, last_name, scope: "user_info", exp}`, TTL 30 días,
HttpOnly. Se setea en `sign_up` y ahora también cuando `sign_in`/`google_sign_in`
bloquean por falta de tarjeta/suscripción.

## Qué hay que hacer

Cambiar checkout para que lea la cookie `user_info` en vez de `access_token`, y
agregar validación del claim `scope` (hoy no se valida — cualquier JWT firmado
con el mismo secreto se acepta sin importar su `scope`).

### 1. `app/utils/token_utils.py`

- **Línea 88**: cambiar el nombre de cookie leído.

  ```python
  # antes
  token = request.cookies.get("access_token")
  # después
  token = request.cookies.get("user_info")
  ```

- **`decode_user_info_token()` (líneas 29-66)**: agregar validación de `scope`
  después de decodificar el JWT. Si `payload.get("scope") != "user_info"`,
  tratarlo igual que un token inválido (devolver `None`, loggear como debug).
  Esto evita aceptar por error un `access_token` real (u otro token de otro
  scope) que llegue firmado con el mismo secreto.

- Actualizar los mensajes de log en líneas 60, 63, 93 que dicen
  `"access_token expired"` / `"access_token invalid"` / `"access_token cookie
  missing/invalid"` para que digan `user_info` — hoy quedarían desalineados
  con el nombre real de la cookie.

- El docstring del módulo (líneas 1-13) y de `require_user_info()`
  (líneas 71-97) ya documentan esto como cookie `user_info` — no necesitan
  cambios, son los que están correctos; el código es el que estaba
  desalineado.

### 2. `routers/checkout.py`

- **Línea 963**: mismo cambio de nombre de cookie.

  ```python
  # antes
  user_info_token = request.cookies.get("access_token")
  # después
  user_info_token = request.cookies.get("user_info")
  ```

- **Línea 992**: actualizar el log `"[CHECKOUT] access_token cookie decoded
  | ..."` → `"[CHECKOUT] user_info cookie decoded | ..."`.

- **Comentarios/docstring en líneas 7, 949, 959** que mencionan
  `access_token`: actualizar a `user_info` para que coincidan con el
  comportamiento real.

### 3. Qué NO cambiar

- No hace falta seguir leyendo `access_token` en ningún lado de este repo:
  se confirmó (grep sobre todo el repo) que `access_token` no se usa acá para
  llamar a la API de `iamatlas` — solo se usaba para identificación local en
  checkout, que es justo lo que `user_info` cubre mejor (trae `name`/
  `last_name`, TTL de 30 días en vez de 15 minutos).
- No tocar `refresh_token` — no se usa en este repo.
- No tocar la cookie `theme` (`checkout.py:53`) ni `tunnel_csrf`
  (`routers/tunnel.py:484`) — no están relacionadas con este flujo.

## Testing

- [ ] Cookie `user_info` válida (payload correcto, `scope: "user_info"`,
      no expirada) → checkout precarga email/nombre y muestra tarjetas
      guardadas, igual que antes con `access_token`.
- [ ] Cookie `user_info` ausente → `require_user_info()` sigue redirigiendo a
      `https://www.iamatlas.do/login` (comportamiento sin cambios).
- [ ] Cookie `user_info` expirada → mismo redirect a login.
- [ ] JWT válido (mismo `SECRET_KEY`) pero con `scope` distinto de
      `"user_info"` (p. ej. un `access_token` real puesto en la cookie
      `user_info` por error) → debe ser rechazado (`decode_user_info_token`
      devuelve `None`), no aceptado.
- [ ] Cookie `access_token` sola (sin `user_info`), aunque tenga el shape
      viejo esperado → ya no debe identificar al usuario (confirma que el
      cambio de nombre de cookie se aplicó).
