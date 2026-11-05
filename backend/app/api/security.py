"""Fail-closed administrator Bearer authentication and independent Exotel Basic authentication."""

import base64
import binascii
import secrets

from fastapi import HTTPException, Request


def require_api_key(request: Request):
    """FastAPI dependency requiring a configured administrator Bearer token for data routes.
    Compare byte strings in constant time; reject missing configuration with 503 and
    missing/incorrect credentials with 401. No tokens or request headers are logged, and
    this pilot token is not a multi-tenant identity system.
    """
    expected = request.app.state.settings.api_key
    if expected is None or not expected.get_secret_value():
        raise HTTPException(503, "API_KEY must be configured for data access")
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or not secrets.compare_digest(
        value[7:].encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def valid_exotel_auth(header: str, settings) -> bool:
    """Validate Exotel's HTTP Basic header against the configured ingress username/password
    before WebSocket upgrade. Return False for absent, malformed, or empty credentials;
    compare decoded UTF-8 byte strings in constant time. This ingress secret is independent
    of the administrator API key and provider control-plane keys.
    """
    if (
        settings.exotel_ws_password is None
        or not settings.exotel_ws_password.get_secret_value()
        or not header.startswith("Basic ")
    ):
        return False
    try:
        value = base64.b64decode(header[6:], validate=True).decode("utf-8")
        user, password = value.split(":", 1)
        return secrets.compare_digest(
            user.encode(), settings.exotel_ws_user.encode()
        ) and secrets.compare_digest(
            password.encode(), settings.exotel_ws_password.get_secret_value().encode()
        )
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
