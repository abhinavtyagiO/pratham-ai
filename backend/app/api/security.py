import base64
import binascii
import secrets

from fastapi import HTTPException, Request


def require_api_key(request: Request):
    expected = request.app.state.settings.api_key
    if expected is None:
        raise HTTPException(503, "API_KEY must be configured for data access")
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or not secrets.compare_digest(
        value[7:], expected.get_secret_value()
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def valid_exotel_auth(header: str, settings) -> bool:
    if settings.exotel_ws_password is None or not header.startswith("Basic "):
        return False
    try:
        value = base64.b64decode(header[6:], validate=True).decode("utf-8")
        user, password = value.split(":", 1)
        return secrets.compare_digest(user, settings.exotel_ws_user) and secrets.compare_digest(
            password, settings.exotel_ws_password.get_secret_value()
        )
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
