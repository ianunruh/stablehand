from __future__ import annotations

import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from stablehand.config import get_settings
from stablehand.models import Integration, Run, Stack
from stablehand.security import decrypt_secret, encrypt_secret, hmac_sha256

logger = logging.getLogger(__name__)

WEBHOOK = "webhook"


def list_integrations(session: Session) -> list[Integration]:
    return list(session.scalars(select(Integration).order_by(Integration.created_at)))


def save_webhook(
    session: Session,
    *,
    name: str,
    url: str,
    secret: str,
    enabled: bool,
    integration: Integration | None = None,
) -> Integration:
    config = {"url": url.strip()}
    if secret.strip():
        config["secret_encrypted"] = encrypt_secret(secret.strip())
    elif integration is not None:
        config["secret_encrypted"] = (integration.config or {}).get("secret_encrypted", "")
    if integration is None:
        integration = Integration(kind=WEBHOOK, name=name.strip(), enabled=enabled, config=config)
        session.add(integration)
    else:
        integration.name = name.strip()
        integration.enabled = enabled
        integration.config = config
    session.commit()
    return integration


def notify_needs_approval(session: Session, run: Run) -> None:
    stack = session.get(Stack, run.stack_id)
    if stack is None:
        return
    payload = {
        "event": "run.needs_approval",
        "run_id": str(run.id),
        "stack_id": str(stack.id),
        "stack_name": stack.name,
        "commit": run.commit_sha,
        "url": f"{get_settings().public_url.rstrip('/')}/runs/{run.id}",
        "counts": _counts(run),
    }
    for integration in session.scalars(select(Integration).where(Integration.enabled.is_(True))):
        if integration.kind != WEBHOOK:
            continue
        _post_webhook(integration, payload)


def _counts(run: Run) -> dict:
    document = run.apply_document or run.check_document or {}
    return document.get("counts") or {
        "hosts": 0,
        "change": 0,
        "unchanged": 0,
        "unreachable": 0,
        "failed": 0,
    }


def _post_webhook(integration: Integration, payload: dict) -> None:
    url = (integration.config or {}).get("url")
    if not url:
        return
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    encrypted = (integration.config or {}).get("secret_encrypted") or ""
    secret = decrypt_secret(encrypted) if encrypted else ""
    if secret:
        headers["X-Stablehand-Signature"] = "sha256=" + hmac_sha256(secret, body)
    try:
        httpx.post(url, content=body, headers=headers, timeout=10)
    except httpx.HTTPError:
        logger.exception("webhook %s failed", integration.name)
