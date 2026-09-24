from __future__ import annotations

import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from stablehand.config import get_settings
from stablehand.models import (
    DeliveryStatus,
    Integration,
    IntegrationDelivery,
    Run,
    Stack,
)
from stablehand.security import decrypt_secret, encrypt_secret, hmac_sha256

WEBHOOK = "webhook"
NEEDS_APPROVAL = "run.needs_approval"


class WebhookError(Exception):
    pass


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
    session.flush()
    return integration


def enqueue_needs_approval(session: Session, run: Run) -> None:
    stack = session.get(Stack, run.stack_id)
    if stack is None:
        return
    if not run.check_fingerprint:
        raise ValueError("A needs-approval run must have a fingerprint.")
    payload = {
        "event": NEEDS_APPROVAL,
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
        existing = session.scalar(
            select(IntegrationDelivery.id).where(
                IntegrationDelivery.integration_id == integration.id,
                IntegrationDelivery.run_id == run.id,
                IntegrationDelivery.event == NEEDS_APPROVAL,
                IntegrationDelivery.fingerprint == run.check_fingerprint,
            )
        )
        if existing is not None:
            continue
        session.add(
            IntegrationDelivery(
                integration_id=integration.id,
                run_id=run.id,
                event=NEEDS_APPROVAL,
                fingerprint=run.check_fingerprint,
                payload=payload,
                status=DeliveryStatus.pending.value,
            )
        )


def _counts(run: Run) -> dict:
    document = run.apply_document or run.check_document or {}
    return document.get("counts") or {
        "hosts": 0,
        "change": 0,
        "unchanged": 0,
        "unreachable": 0,
        "failed": 0,
    }


def send_delivery(delivery: IntegrationDelivery, integration: Integration) -> None:
    url = (integration.config or {}).get("url")
    if not url:
        raise WebhookError("Webhook URL is missing.")
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Stablehand-Delivery": str(delivery.id),
    }
    encrypted = (integration.config or {}).get("secret_encrypted") or ""
    secret = decrypt_secret(encrypted) if encrypted else ""
    if secret:
        headers["X-Stablehand-Signature"] = "sha256=" + hmac_sha256(secret, body)
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=10)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WebhookError(str(exc)) from exc
