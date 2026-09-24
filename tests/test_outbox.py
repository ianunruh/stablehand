from unittest.mock import Mock, patch

import httpx
from sqlalchemy import select

from stablehand.integrations.service import enqueue_needs_approval, save_webhook
from stablehand.models import (
    DeliveryStatus,
    IntegrationDelivery,
    RunState,
)
from stablehand.runs.service import claim, create_run, ingest_check_result
from stablehand.security import aware, utcnow
from stablehand.worker import _deliver_webhooks
from tests.conftest import make_stack
from tests.test_normalize import FIXTURE, STDERR


def _delivery(db) -> IntegrationDelivery:
    stack = make_stack()
    db.add(stack)
    db.flush()
    run = create_run(db, stack, commit_sha="abc", trigger="ci", user=None)
    assert claim(db, run, RunState.check_queued, RunState.check_running)
    save_webhook(
        db,
        name="ops",
        url="https://example.test/hook",
        secret="sekret",
        enabled=True,
    )
    db.commit()
    ingest_check_result(
        db,
        run,
        raw=FIXTURE,
        inventory_hosts=["web-1", "web-2"],
        stderr=STDERR,
        exit_code=0,
    )
    db.commit()
    delivery = db.scalar(select(IntegrationDelivery))
    assert delivery is not None
    return delivery


def test_needs_approval_enqueues_redacted_delivery(db):
    delivery = _delivery(db)

    assert delivery.status == DeliveryStatus.pending.value
    assert delivery.payload["event"] == "run.needs_approval"
    assert "diffs" not in delivery.payload

    run = delivery.run
    enqueue_needs_approval(db, run)
    db.flush()
    assert len(list(db.scalars(select(IntegrationDelivery)))) == 1


def test_delivery_rolls_back_with_run_transition(db):
    delivery = _delivery(db)
    run = delivery.run
    db.delete(delivery)
    run.state = RunState.check_running.value
    db.commit()

    ingest_check_result(
        db,
        run,
        raw=FIXTURE,
        inventory_hosts=["web-1", "web-2"],
        stderr=STDERR,
        exit_code=0,
    )
    db.rollback()

    db.refresh(run)
    assert run.state == RunState.check_running.value
    assert db.scalar(select(IntegrationDelivery)) is None


def test_delivery_sends_signature_and_delivery_id(db):
    delivery = _delivery(db)
    response = Mock()
    response.raise_for_status.return_value = None

    with patch("stablehand.integrations.service.httpx.post", return_value=response) as post:
        _deliver_webhooks(db)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.delivered.value
    headers = post.call_args.kwargs["headers"]
    assert headers["X-Stablehand-Delivery"] == str(delivery.id)
    assert headers["X-Stablehand-Signature"].startswith("sha256=")


def test_delivery_retries_then_fails(db):
    delivery = _delivery(db)
    delivery.attempt_count = 9
    db.commit()

    with patch(
        "stablehand.integrations.service.httpx.post",
        side_effect=httpx.ConnectError("offline"),
    ):
        _deliver_webhooks(db)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.failed.value
    assert delivery.attempt_count == 10
    assert "offline" in (delivery.last_error or "")
    assert aware(delivery.next_attempt_at) > utcnow()


def test_delivery_reschedules_transient_failure(db):
    delivery = _delivery(db)
    attempted_at = utcnow()
    request = httpx.Request("POST", "https://example.test/hook")
    response = httpx.Response(503, request=request)

    with patch(
        "stablehand.integrations.service.httpx.post",
        return_value=response,
    ):
        _deliver_webhooks(db)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.pending.value
    assert delivery.attempt_count == 1
    assert aware(delivery.next_attempt_at) > attempted_at
