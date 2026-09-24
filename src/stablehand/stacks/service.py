from __future__ import annotations

import uuid
from datetime import datetime

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from stablehand.models import ApiToken, ExecutorKind, Stack, StackApprover, User
from stablehand.security import after, hash_token, new_token, token_prefix, utcnow


class StackError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate_stack(
    *,
    name: str,
    deploy_file: str,
    inventory: str,
    executor: str,
    git_url: str,
    git_ref: str,
    local_path: str,
    schedule_cron: str,
) -> None:
    if not name.strip():
        raise StackError("Name is required.")
    if not deploy_file.strip() or not inventory.strip():
        raise StackError("Deploy file and inventory are required.")
    if executor not in {ExecutorKind.local.value, ExecutorKind.kubernetes.value}:
        raise StackError("Choose a local or Kubernetes executor.")
    if executor == ExecutorKind.kubernetes.value and not git_url.strip():
        raise StackError("Kubernetes stacks require a git URL.")
    if not git_url.strip() and not local_path.strip():
        raise StackError("Provide a git URL or a local path.")
    if schedule_cron.strip():
        try:
            croniter(schedule_cron.strip(), utcnow())
        except (ValueError, KeyError) as exc:
            raise StackError("Schedule is not a valid cron expression.") from exc


def save_stack(
    session: Session,
    *,
    stack: Stack | None,
    name: str,
    deploy_file: str,
    inventory: str,
    executor: str,
    git_url: str,
    git_ref: str,
    local_path: str,
    secret_ref: str,
    schedule_cron: str,
    approver_user_ids: list[str],
    approver_groups: str,
) -> Stack:
    validate_stack(
        name=name,
        deploy_file=deploy_file,
        inventory=inventory,
        executor=executor,
        git_url=git_url,
        git_ref=git_ref,
        local_path=local_path,
        schedule_cron=schedule_cron,
    )
    if stack is None:
        stack = Stack(name=name.strip())
        session.add(stack)
    stack.name = name.strip()
    stack.deploy_file = deploy_file.strip()
    stack.inventory = inventory.strip()
    stack.executor = executor
    stack.git_url = git_url.strip() or None
    stack.git_ref = (git_ref.strip() or "main") if stack.git_url else None
    stack.local_path = local_path.strip() or None
    stack.secret_ref = secret_ref.strip() or None
    stack.schedule_cron = schedule_cron.strip() or None
    if stack.schedule_cron and stack.schedule_next_at is None:
        stack.schedule_next_at = next_schedule(stack.schedule_cron, utcnow())
    if not stack.schedule_cron:
        stack.schedule_next_at = None
    stack.updated_at = utcnow()
    stack.approvers.clear()
    for user_id in approver_user_ids:
        if user_id:
            stack.approvers.append(StackApprover(user_id=uuid.UUID(str(user_id))))
    for group in _groups(approver_groups):
        stack.approvers.append(StackApprover(group_name=group))
    session.commit()
    session.refresh(stack)
    return stack


def next_schedule(expression: str, now: datetime) -> datetime:
    upcoming = croniter(expression, now).get_next(datetime)
    if upcoming.tzinfo is None:
        return upcoming.replace(tzinfo=now.tzinfo)
    return upcoming


def issue_ci_token(
    session: Session, stack: Stack, user: User, name: str, expires_days: int | None
) -> str:
    if not name.strip():
        raise StackError("Token name is required.")
    plaintext = new_token("shc_")
    session.add(
        ApiToken(
            stack_id=stack.id,
            name=name.strip(),
            token_hash=hash_token(plaintext),
            token_prefix=token_prefix(plaintext),
            expires_at=after(days=expires_days) if expires_days else None,
            created_by_id=user.id,
        )
    )
    session.commit()
    return plaintext


def revoke_ci_token(session: Session, token_id: uuid.UUID) -> None:
    token = session.get(ApiToken, token_id)
    if token is None or token.revoked_at is not None:
        return
    token.revoked_at = utcnow()
    session.commit()


def user_choices(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.email)))


def _groups(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
