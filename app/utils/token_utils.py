"""
Token utilities — decode the `user_info` JWT cookie set by the auth service.

The auth service (api.iamatlas.do) sets an HttpOnly cookie named ``user_info``
containing a JWT with the logged-in user's profile data.  This module provides
a safe decoder so the payments service can identify the user without requiring
a separate auth call.

Configuration (.env)
--------------------
SECRET_KEY   The HMAC-SHA256 secret shared with the auth service.
ALGORITHM    JWT algorithm (default: HS256).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import jwt

logger = logging.getLogger(__name__)

_SECRET_KEY = os.getenv("SECRET_KEY", "")
_ALGORITHM = os.getenv("ALGORITHM", "HS256")


def decode_user_info_token(token: str | None) -> dict[str, Any] | None:
    """Decode and validate a ``user_info`` JWT cookie.

    Returns the payload dict on success, or ``None`` if the token is
    missing, expired, has a wrong scope, or is otherwise invalid.

    The payload contains::

        {
            "sub": "42",           # user ID
            "email": "user@x.com",
            "name": "Juan",
            "last_name": "Pérez",
            "scope": "user_info",
            "exp": 1753000000
        }
    """
    if not token:
        return None

    if not _SECRET_KEY:
        logger.warning("[token_utils] SECRET_KEY not configured — cannot decode user_info cookie")
        return None

    try:
        payload = jwt.decode(
            token,
            _SECRET_KEY,
            algorithms=[_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        logger.debug("[token_utils] user_info token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("[token_utils] user_info token invalid: %s", exc)
        return None

    # Verify scope — only accept user_info tokens
    if payload.get("scope") != "user_info":
        logger.debug("[token_utils] user_info token has wrong scope: %s", payload.get("scope"))
        return None

    return payload
