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
        logger.debug("[token_utils] access_token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("[token_utils] access_token invalid: %s", exc)
        return None

    return payload


from fastapi import Request

async def require_user_info(request: Request) -> dict[str, Any]:
    """FastAPI dependency — require a valid ``user_info`` cookie.

    If the cookie is missing, expired, or invalid, redirects the user
    to the Atlas login page instead of returning a JSON 401.

    Usage::

        from app.utils.token_utils import require_user_info

        @router.get("/checkout")
        async def checkout(user=Depends(require_user_info)):
            # user is the decoded JWT payload dict
            email = user["email"]
    """
    from fastapi.responses import RedirectResponse

    token = request.cookies.get("access_token")
    user_data = decode_user_info_token(token)

    if user_data is None:
        logger.warning(
            "[token_utils] ✗ access_token cookie missing/invalid — redirecting to login"
        )
        raise _LoginRedirectException()

    return user_data


class _LoginRedirectException(Exception):
    """Raised when user_info cookie is missing — caught by exception handler."""
    pass


def register_login_redirect_handler(app) -> None:
    """Register the exception handler on the FastAPI app.

    Call this once in main.py::

        from app.utils.token_utils import register_login_redirect_handler
        register_login_redirect_handler(app)
    """
    from fastapi.responses import RedirectResponse

    @app.exception_handler(_LoginRedirectException)
    async def _handle_login_redirect(request, exc):
        login_url = "https://www.iamatlas.do/login"
        return RedirectResponse(url=login_url, status_code=302)
