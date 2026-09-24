from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from stablehand.config import get_settings

_passwords = PasswordHash.recommended()


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def hash_password(password: str) -> str:
    return _passwords.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _passwords.verify(password, password_hash)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def token_prefix(token: str) -> str:
    return token[:12]


def csrf_token(session_id: str) -> str:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, session_id.encode(), hashlib.sha256).hexdigest()


def hmac_sha256(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def csrf_matches(session_id: str, provided: str) -> bool:
    if not session_id or not provided:
        return False
    expected = csrf_token(session_id)
    return hmac.compare_digest(expected, provided)


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode()).decode()


def after(hours: int = 0, days: int = 0) -> datetime:
    return utcnow() + timedelta(hours=hours, days=days)
