from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from authlib.jose import jwt
from authlib.jose.errors import JoseError

from stablehand.config import get_settings
from stablehand.models import OidcSettings
from stablehand.security import decrypt_secret


class OidcError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def authorization_redirect(settings_row: OidcSettings, state: str) -> str:
    document = discovery(settings_row.issuer)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings_row.client_id,
            "redirect_uri": redirect_uri(),
            "scope": settings_row.scopes or "openid email profile",
            "state": state,
        }
    )
    return f"{document['authorization_endpoint']}?{query}"


def exchange_identity(settings_row: OidcSettings, code: str) -> tuple[str, str, str]:
    document = discovery(settings_row.issuer)
    secret = decrypt_secret(settings_row.client_secret_encrypted)
    try:
        token_response = httpx.post(
            document["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
                "client_id": settings_row.client_id,
                "client_secret": secret,
            },
            timeout=15,
        )
        token_response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OidcError("The identity provider rejected the login.") from exc
    payload = token_response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise OidcError("The identity provider did not return an ID token.")
    claims = _verified_claims(id_token, document, settings_row)
    email = claims.get("email")
    name = claims.get("name") or ""
    if not email and payload.get("access_token") and document.get("userinfo_endpoint"):
        info = httpx.get(
            document["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {payload['access_token']}"},
            timeout=15,
        )
        info.raise_for_status()
        profile = info.json()
        email = profile.get("email")
        name = name or profile.get("name") or ""
    if not email:
        raise OidcError("The identity provider did not include an email address.")
    return str(claims["sub"]), str(email), str(name or "")


def new_state() -> str:
    return secrets.token_urlsafe(24)


def discovery(issuer: str) -> dict:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OidcError("Could not read the identity provider configuration.") from exc
    return response.json()


def redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/login/oidc/callback"


def _verified_claims(id_token: str, document: dict, settings_row: OidcSettings) -> dict:
    try:
        jwks = httpx.get(document["jwks_uri"], timeout=15)
        jwks.raise_for_status()
        claims = jwt.decode(id_token, jwks.json())
        claims.validate()
    except (httpx.HTTPError, JoseError, ValueError) as exc:
        raise OidcError("Could not validate the identity token.") from exc
    issuer = str(claims.get("iss", "")).rstrip("/")
    if issuer != settings_row.issuer.rstrip("/"):
        raise OidcError("Identity token issuer does not match the configured issuer.")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings_row.client_id not in audiences:
        raise OidcError("Identity token audience does not match the client id.")
    if not claims.get("sub"):
        raise OidcError("Identity token is missing a subject.")
    return dict(claims)
