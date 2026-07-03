# Documentación del Endpoint: Marcar Tarjeta Predeterminada

**Servicio:** Tokens / DataVault (Microservicio Azul Pagos)  
**Ruta:** `PUT /api/v1/tokens/{customer_id}/default/{card_id}`  
**Propósito:** Permite cambiar la tarjeta principal de cobro de un cliente. Le quita el estado de "predeterminada" a cualquier otra tarjeta que tuviera el usuario y se lo asigna únicamente a la tarjeta indicada.

---

## 1. Parámetros de Ruta (URL)

| Parámetro     | Tipo   | Descripción |
|--------------|--------|-------------|
| `customer_id` | `string` | El ID del usuario en Atlas (usualmente el UUID o email). Es el mismo identificador utilizado al crear/guardar la tarjeta. |
| `card_id`     | `string` | El ID interno de la tarjeta en la base de datos de pagos (no confundir con el token de DataVault). Es el campo `id` devuelto al listar las tarjetas. |

---

## 2. Ejemplo de Petición (Request)

Al ser una petición `PUT` con parámetros en la URL, no se requiere el envío de información en el cuerpo (`body`) del mensaje.

```http
PUT /api/v1/tokens/usr_12345/default/8a9f1bad-802c-4f64-af54-129bcaab742a
Content-Type: application/json
```

---

## 3. Respuestas Esperadas (Responses)

### ✅ 200 OK (Éxito)
La tarjeta ha sido marcada exitosamente como la predeterminada del usuario.
```json
{
  "message": "Tarjeta predeterminada actualizada exitosamente."
}
```

### ❌ 403 Forbidden (Error de Autorización/Seguridad)
Se produce si la tarjeta indicada (`card_id`) existe, pero está asociada a un cliente distinto al proveído (`customer_id`).
```json
{
  "detail": "Card '8a9f1bad...' does not belong to customer 'usr_12345'."
}
```

### ❌ 404 Not Found (Error de Tarjeta Inexistente)
Se produce si el ID de la tarjeta (`card_id`) no existe en el registro de la base de datos.
```json
{
  "detail": "Card '8a9f1bad...' not found."
}
```

---

## 4. Notas Técnicas y de Flujo
1. **Auto-asignación inicial:** Cuando un usuario guarda una tarjeta y no tiene ninguna otra tarjeta registrada en su cuenta, el sistema asignará esa primera tarjeta como predeterminada automáticamente sin necesidad de llamar a este endpoint.
2. **Uso en pagos:** Todas las integraciones internas del sistema (como los cobros de utilidades o suscripciones con tarjetas guardadas) buscarán automáticamente la tarjeta que tenga `is_default == True`.
3. **Persistencia:** Al llamar a este endpoint, se actualizan los registros de base de datos (`pagos.saved_cards`) transaccionalmente, asegurando que solo exista un (1) registro con la bandera de predeterminado activo por cliente.
