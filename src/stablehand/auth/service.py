from __future__ import annotations

import secrets

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stablehand.models import OidcSettings, Role, User
from stablehand.models import Session as AuthSession
from stablehand.security import after, aware, encrypt_secret, utcnow, verify_password

SESSION_COOKIE = "stablehand_session"
SESSION_DAYS = 14


class AuthError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        raise AuthError("Email or password is incorrect.")
    if not user.enabled:
        raise AuthError("This account is disabled.")
    return user


def start_session(db: Session, user: User) -> AuthSession:
    row = AuthSession(
        id=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=after(days=SESSION_DAYS),
    )
    db.add(row)
    db.commit()
    return row


def current_user(request: Request, db: Session) -> User | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    row = db.get(AuthSession, session_id)
    if row is None or aware(row.expires_at) <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.enabled:
        return None
    return user


def logout(db: Session, request: Request) -> None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return
    row = db.get(AuthSession, session_id)
    if row is not None:
        db.delete(row)
        db.commit()


def ensure_oidc(db: Session) -> OidcSettings:
    row = db.get(OidcSettings, 1)
    if row is None:
        row = OidcSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def save_oidc(
    db: Session,
    *,
    enabled: bool,
    issuer: str,
    client_id: str,
    client_secret: str,
    scopes: str,
) -> None:
    row = ensure_oidc(db)
    row.enabled = enabled
    row.issuer = issuer.strip().rstrip("/")
    row.client_id = client_id.strip()
    row.scopes = scopes.strip() or "openid email profile"
    if client_secret.strip():
        row.client_secret_encrypted = encrypt_secret(client_secret.strip())
    db.commit()


def update_user(
    db: Session,
    user: User,
    actor: User,
    *,
    enabled: bool,
    role: str,
    groups: list[str],
) -> None:
    if role not in {Role.member.value, Role.admin.value}:
        raise AuthError("Unknown role.")
    removing_admin = (
        user.role == Role.admin.value and user.enabled and (not enabled or role != Role.admin.value)
    )
    if removing_admin and _enabled_admins(db) <= 1:
        raise AuthError("Keep at least one enabled admin.")
    if actor.id == user.id and not enabled:
        raise AuthError("You cannot disable your own account.")
    user.enabled = enabled
    user.role = role
    user.groups = groups
    db.commit()


def accept_oidc_user(db: Session, *, subject: str, email: str, name: str) -> User:
    email = email.strip().lower()
    existing = db.scalar(select(User).where(User.oidc_subject == subject))
    if existing is None:
        existing = db.scalar(select(User).where(User.email == email))
    if existing is None:
        existing = User(
            email=email,
            name=name or email,
            oidc_subject=subject,
            role=Role.member.value,
            groups=[],
            enabled=False,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing
    if existing.oidc_subject is None:
        existing.oidc_subject = subject
    if name and not existing.name:
        existing.name = name
    db.commit()
    return existing


def _enabled_admins(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == Role.admin.value, User.enabled.is_(True))
        )
        or 0
    )
