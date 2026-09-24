from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from stablehand.config import get_settings
from stablehand.db import get_session
from stablehand.models import ApiToken, Run, Stack, Trigger
from stablehand.runs.service import (
    APPLY,
    CHECK,
    RunError,
    append_log,
    counts_of,
    create_run,
    ingest_apply_precheck,
    ingest_apply_result,
    ingest_check_result,
    lookup_run_token,
)
from stablehand.security import aware, hash_token, utcnow
from stablehand.source import SourceError, resolve_commit

router = APIRouter(prefix="/api")


class RunCreate(BaseModel):
    commit: str | None = None
    limit: str | None = None


class LogIn(BaseModel):
    phase: str
    text: str


class ResultIn(BaseModel):
    raw: dict | None = None
    inventory_hosts: list[str] = []
    stderr: str = ""
    exit_code: int = 0


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return header.split(" ", 1)[1].strip()


def _ci_token(request: Request, db: Session, stack_id: uuid.UUID) -> ApiToken:
    plaintext = _bearer(request)
    if not plaintext.startswith("shc_"):
        raise HTTPException(status_code=401, detail="CI token required.")
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(plaintext)))
    if token is None or token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="CI token required.")
    if token.expires_at is not None and aware(token.expires_at) <= utcnow():
        raise HTTPException(status_code=401, detail="CI token expired.")
    if token.stack_id != stack_id:
        raise HTTPException(status_code=403, detail="Token is not valid for this stack.")
    return token


def _run_from_token(request: Request, db: Session, run_id: uuid.UUID, phase: str) -> Run:
    plaintext = _bearer(request)
    if not plaintext.startswith("shr_"):
        raise HTTPException(status_code=401, detail="Run token required.")
    run = lookup_run_token(db, plaintext, phase)
    if run is None or run.id != run_id:
        raise HTTPException(status_code=401, detail="Run token required.")
    return run


@router.post("/stacks/{stack_id}/runs", status_code=201)
def open_run(
    stack_id: uuid.UUID,
    body: RunCreate,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    _ci_token(request, db, stack_id)
    stack = db.get(Stack, stack_id)
    if stack is None:
        raise HTTPException(status_code=404, detail="Stack not found.")
    commit = (body.commit or "").strip()
    if not commit:
        try:
            commit = resolve_commit(stack)
        except SourceError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc
    try:
        run = create_run(
            db, stack, commit_sha=commit, trigger=Trigger.ci, user=None, limit=body.limit
        )
    except RunError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return _public_run(run)


@router.get("/runs/{run_id}")
def read_run(run_id: uuid.UUID, request: Request, db: Session = Depends(get_session)) -> dict:
    plaintext = _bearer(request)
    if not plaintext.startswith("shc_"):
        raise HTTPException(status_code=401, detail="CI token required.")
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(plaintext)))
    if token is None or token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="CI token required.")
    if token.expires_at is not None and aware(token.expires_at) <= utcnow():
        raise HTTPException(status_code=401, detail="CI token expired.")
    run = db.scalar(select(Run).where(Run.id == run_id).options(selectinload(Run.stack)))
    if run is None or run.stack_id != token.stack_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _public_run(run)


@router.post("/runs/{run_id}/logs", status_code=204)
def write_logs(
    run_id: uuid.UUID,
    body: LogIn,
    request: Request,
    db: Session = Depends(get_session),
) -> None:
    phase = body.phase if body.phase in {CHECK, APPLY} else ""
    if not phase:
        raise HTTPException(status_code=422, detail="Unknown phase.")
    run = _run_from_token(request, db, run_id, phase)
    append_log(db, run, phase, body.text)


@router.post("/runs/{run_id}/check-result")
def check_result(
    run_id: uuid.UUID,
    body: ResultIn,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    run = _run_from_token(request, db, run_id, CHECK)
    try:
        ingest_check_result(
            db,
            run,
            raw=body.raw,
            inventory_hosts=body.inventory_hosts,
            stderr=body.stderr,
            exit_code=body.exit_code,
        )
    except RunError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return {"state": run.state}


@router.post("/runs/{run_id}/apply-precheck")
def apply_precheck(
    run_id: uuid.UUID,
    body: ResultIn,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    run = _run_from_token(request, db, run_id, APPLY)
    try:
        proceed = ingest_apply_precheck(
            db,
            run,
            raw=body.raw,
            inventory_hosts=body.inventory_hosts,
            stderr=body.stderr,
            exit_code=body.exit_code,
        )
    except RunError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return {"proceed": proceed, "state": run.state}


@router.post("/runs/{run_id}/apply-result")
def apply_result(
    run_id: uuid.UUID,
    body: ResultIn,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    run = _run_from_token(request, db, run_id, APPLY)
    try:
        ingest_apply_result(
            db,
            run,
            raw=body.raw,
            inventory_hosts=body.inventory_hosts,
            stderr=body.stderr,
            exit_code=body.exit_code,
        )
    except RunError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return {"state": run.state}


def _public_run(run: Run) -> dict:
    settings = get_settings()
    return {
        "id": str(run.id),
        "stack_id": str(run.stack_id),
        "state": run.state,
        "commit": run.commit_sha,
        "limit": run.limit,
        "trigger": run.trigger,
        "counts": counts_of(run),
        "url": f"{settings.public_url.rstrip('/')}/runs/{run.id}",
    }
