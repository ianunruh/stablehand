from __future__ import annotations

import hmac
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from stablehand.auth.oidc import OidcError, authorization_redirect, exchange_identity, new_state
from stablehand.auth.service import (
    SESSION_COOKIE,
    SESSION_DAYS,
    AuthError,
    accept_oidc_user,
    authenticate,
    current_user,
    ensure_oidc,
    logout,
    save_oidc,
    start_session,
    update_user,
)
from stablehand.config import get_settings
from stablehand.db import get_session
from stablehand.integrations.service import list_integrations, save_webhook
from stablehand.models import (
    ApiToken,
    Integration,
    Role,
    Run,
    RunState,
    Runtime,
    Source,
    Stack,
    User,
)
from stablehand.runs.service import (
    RunError,
    Trigger,
    approval_error,
    approve_run,
    create_run,
    reject_run,
)
from stablehand.runtimes.service import RuntimeConfigError, delete_runtime, save_runtime
from stablehand.source import SourceError as CheckoutError
from stablehand.source import resolve_commit
from stablehand.sources.service import SourceError, delete_source, save_source
from stablehand.stacks.service import (
    StackError,
    issue_ci_token,
    load_run_stack,
    load_stack,
    revoke_ci_token,
    save_stack,
    user_choices,
)
from stablehand.ui.render import render

router = APIRouter()
TOKEN_COOKIE = "stablehand_token_flash"
OIDC_COOKIE = "stablehand_oidc_state"


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def require_user(request: Request, db: Session = Depends(get_session)) -> User:
    user = current_user(request, db)
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != Role.admin.value:
        raise Forbidden()
    return user


def _redirect(path: str, *, notice: str = "", error: str = "") -> RedirectResponse:
    if notice:
        path = f"{path}?notice={quote(notice)}"
    elif error:
        path = f"{path}?error={quote(error)}"
    return RedirectResponse(path, status_code=303)


def _session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
    )


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_session)):
    if current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    oidc = ensure_oidc(db)
    return render(request, "login.html", oidc_enabled=oidc.enabled, user=None)


@router.post("/login")
def login_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Session = Depends(get_session),
):
    try:
        user = authenticate(db, email, password)
    except AuthError as exc:
        oidc = ensure_oidc(db)
        return render(
            request,
            "login.html",
            oidc_enabled=oidc.enabled,
            user=None,
            error=exc.message,
            status_code=400,
        )
    response = RedirectResponse("/", status_code=303)
    _session_cookie(response, start_session(db, user).id)
    return response


@router.post("/logout")
def logout_submit(request: Request, db: Session = Depends(get_session)):
    logout(db, request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/login/oidc")
def oidc_start(db: Session = Depends(get_session)):
    row = ensure_oidc(db)
    if not row.enabled:
        return _redirect("/login", error="OIDC is not enabled.")
    state = new_state()
    try:
        location = authorization_redirect(row, state)
    except OidcError as exc:
        return _redirect("/login", error=exc.message)
    response = RedirectResponse(location, status_code=303)
    response.set_cookie(
        OIDC_COOKIE,
        state,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


@router.get("/login/oidc/callback")
def oidc_callback(
    request: Request,
    db: Session = Depends(get_session),
    code: str = "",
    state: str = "",
):
    expected = request.cookies.get(OIDC_COOKIE, "")
    if not expected or not state or not hmac.compare_digest(expected, state):
        return _redirect("/login", error="OIDC state did not match.")
    row = ensure_oidc(db)
    try:
        subject, email, name = exchange_identity(row, code)
    except OidcError as exc:
        return _redirect("/login", error=exc.message)
    user = accept_oidc_user(db, subject=subject, email=email, name=name)
    if not user.enabled:
        response = _redirect(
            "/login", notice="An admin must enable this account before you can sign in."
        )
        response.delete_cookie(OIDC_COOKIE, path="/")
        return response
    response = RedirectResponse("/", status_code=303)
    _session_cookie(response, start_session(db, user).id)
    response.delete_cookie(OIDC_COOKIE, path="/")
    return response


@router.get("/")
def inbox(request: Request, user: User = Depends(require_user), db: Session = Depends(get_session)):
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.state == RunState.needs_approval.value)
            .options(selectinload(Run.stack), selectinload(Run.trigger_user))
            .order_by(Run.created_at.desc())
        )
    )
    return render(request, "inbox.html", user=user, runs=runs)


@router.get("/stacks")
def stack_list(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_session)
):
    stacks = list(db.scalars(select(Stack).order_by(Stack.name).options(*load_stack())))
    latest: dict = {}
    for stack in stacks:
        latest[stack.id] = db.scalar(
            select(Run).where(Run.stack_id == stack.id).order_by(Run.created_at.desc())
        )
    return render(request, "stacks/list.html", user=user, stacks=stacks, latest=latest)


@router.get("/stacks/new")
def stack_new(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_session)
):
    return render(
        request,
        "stacks/form.html",
        user=user,
        stack=None,
        **_stack_form_context(db, None),
    )


@router.post("/stacks")
def stack_create(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    deploy_file: Annotated[str, Form()] = "",
    inventory: Annotated[str, Form()] = "",
    default_limit: Annotated[str, Form()] = "",
    source_id: Annotated[str, Form()] = "",
    runtime_id: Annotated[str, Form()] = "",
    git_ref: Annotated[str, Form()] = "",
    schedule_cron: Annotated[str, Form()] = "",
    approver_groups: Annotated[str, Form()] = "",
    approver_user_id: Annotated[list[str] | None, Form()] = None,
):
    try:
        stack = save_stack(
            db,
            stack=None,
            name=name,
            deploy_file=deploy_file,
            inventory=inventory,
            default_limit=default_limit,
            source_id=source_id,
            runtime_id=runtime_id,
            git_ref=git_ref,
            schedule_cron=schedule_cron,
            approver_user_ids=approver_user_id or [],
            approver_groups=approver_groups,
        )
    except (StackError, ValueError) as exc:
        db.rollback()
        message = (
            exc.message if isinstance(exc, StackError) else "Choose approvers from the user list."
        )
        return render(
            request,
            "stacks/form.html",
            user=user,
            stack=None,
            error=message,
            form=_form_state(locals()),
            status_code=400,
            **_stack_form_context(
                db,
                None,
                selected_users=set(approver_user_id or []),
                group_text=approver_groups,
                source_id=source_id,
            ),
        )
    return _redirect(f"/stacks/{stack.id}", notice="Stack saved.")


@router.get("/stacks/{stack_id}")
def stack_detail(
    stack_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    stack = _stack_or_404(db, stack_id)
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.stack_id == stack.id)
            .options(selectinload(Run.trigger_user))
            .order_by(Run.created_at.desc())
            .limit(30)
        )
    )
    return render(request, "stacks/detail.html", user=user, stack=stack, runs=runs)


@router.get("/stacks/{stack_id}/edit")
def stack_edit(
    stack_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    stack = _stack_or_404(db, stack_id)
    return render(
        request, "stacks/form.html", user=user, stack=stack, **_stack_form_context(db, stack)
    )


@router.post("/stacks/{stack_id}")
def stack_update(
    stack_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    deploy_file: Annotated[str, Form()] = "",
    inventory: Annotated[str, Form()] = "",
    default_limit: Annotated[str, Form()] = "",
    source_id: Annotated[str, Form()] = "",
    runtime_id: Annotated[str, Form()] = "",
    git_ref: Annotated[str, Form()] = "",
    schedule_cron: Annotated[str, Form()] = "",
    approver_groups: Annotated[str, Form()] = "",
    approver_user_id: Annotated[list[str] | None, Form()] = None,
):
    stack = _stack_or_404(db, stack_id)
    try:
        save_stack(
            db,
            stack=stack,
            name=name,
            deploy_file=deploy_file,
            inventory=inventory,
            default_limit=default_limit,
            source_id=source_id,
            runtime_id=runtime_id,
            git_ref=git_ref,
            schedule_cron=schedule_cron,
            approver_user_ids=approver_user_id or [],
            approver_groups=approver_groups,
        )
    except (StackError, ValueError) as exc:
        db.rollback()
        message = (
            exc.message if isinstance(exc, StackError) else "Choose approvers from the user list."
        )
        return render(
            request,
            "stacks/form.html",
            user=user,
            stack=stack,
            error=message,
            form=_form_state(locals()),
            status_code=400,
            **_stack_form_context(
                db,
                stack,
                selected_users=set(approver_user_id or []),
                group_text=approver_groups,
                source_id=source_id,
            ),
        )
    return _redirect(f"/stacks/{stack.id}", notice="Stack saved.")


@router.post("/stacks/{stack_id}/runs")
def stack_run(
    stack_id: uuid.UUID,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
    limit: Annotated[str | None, Form()] = None,
):
    stack = _stack_or_404(db, stack_id)
    try:
        commit = resolve_commit(stack)
        run = create_run(
            db, stack, commit_sha=commit, trigger=Trigger.manual, user=user, limit=limit
        )
    except (CheckoutError, RunError) as exc:
        db.rollback()
        return _redirect(f"/stacks/{stack.id}", error=exc.message)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/stacks/{stack_id}/tokens")
def token_list(
    stack_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    stack = _stack_or_404(db, stack_id)
    tokens = list(
        db.scalars(
            select(ApiToken)
            .where(ApiToken.stack_id == stack.id)
            .order_by(ApiToken.created_at.desc())
        )
    )
    revealed = request.cookies.get(TOKEN_COOKIE, "")
    response = render(
        request,
        "stacks/tokens.html",
        user=user,
        stack=stack,
        tokens=tokens,
        revealed=revealed,
    )
    if revealed:
        response.delete_cookie(TOKEN_COOKIE, path="/")
    return response


@router.post("/stacks/{stack_id}/tokens")
def token_create(
    stack_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    expires_days: Annotated[str, Form()] = "",
):
    stack = _stack_or_404(db, stack_id)
    days = int(expires_days) if expires_days.strip().isdigit() else None
    try:
        plaintext = issue_ci_token(db, stack, user, name, days)
    except StackError as exc:
        db.rollback()
        return _redirect(f"/stacks/{stack.id}/tokens", error=exc.message)
    response = _redirect(
        f"/stacks/{stack.id}/tokens", notice="Copy the token now. It will not be shown again."
    )
    response.set_cookie(
        TOKEN_COOKIE,
        plaintext,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        max_age=120,
        path="/",
    )
    return response


@router.post("/tokens/{token_id}/revoke")
def token_revoke(
    token_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    del user
    token = db.get(ApiToken, token_id)
    if token is None:
        return _redirect("/stacks", error="Token not found.")
    stack_id = token.stack_id
    revoke_ci_token(db, token_id)
    return _redirect(f"/stacks/{stack_id}/tokens", notice="Token revoked.")


@router.get("/runs/{run_id}")
def run_detail(
    run_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    run = _run_or_404(db, run_id)
    show_all = request.query_params.get("all") == "1"
    return render(
        request,
        "runs/detail.html",
        user=user,
        run=run,
        stack=run.stack,
        show_all=show_all,
        can_approve=approval_error(run, user, run.stack) is None,
        approve_block=approval_error(run, user, run.stack),
    )


@router.get("/runs/{run_id}/body")
def run_body(
    run_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    run = _run_or_404(db, run_id)
    show_all = request.query_params.get("all") == "1"
    return render(
        request,
        "runs/body.html",
        user=user,
        run=run,
        stack=run.stack,
        show_all=show_all,
        can_approve=approval_error(run, user, run.stack) is None,
        approve_block=approval_error(run, user, run.stack),
    )


@router.post("/runs/{run_id}/approve")
def run_approve(
    run_id: uuid.UUID,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    run = _run_or_404(db, run_id)
    try:
        approve_run(db, run, user, run.stack)
    except RunError as exc:
        db.rollback()
        return _redirect(f"/runs/{run.id}", error=exc.message)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/reject")
def run_reject(
    run_id: uuid.UUID,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
    reason: Annotated[str, Form()] = "",
):
    run = _run_or_404(db, run_id)
    try:
        reject_run(db, run, user, run.stack, reason)
    except RunError as exc:
        db.rollback()
        return _redirect(f"/runs/{run.id}", error=exc.message)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/sources")
def source_list(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_session)
):
    sources = list(db.scalars(select(Source).order_by(Source.name)))
    return render(
        request,
        "sources/list.html",
        user=user,
        sources=sources,
        usage=_usage(db, Stack.source_id),
    )


@router.get("/sources/new")
def source_new(request: Request, user: User = Depends(require_admin)):
    return render(request, "sources/form.html", user=user, source=None)


@router.post("/sources")
def source_create(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    git_url: Annotated[str, Form()] = "",
    git_ref: Annotated[str, Form()] = "",
    local_path: Annotated[str, Form()] = "",
    git_ssh_key: Annotated[str, Form()] = "",
    clear_git_ssh_key: Annotated[str, Form()] = "",
):
    try:
        source = save_source(
            db,
            source=None,
            name=name,
            git_url=git_url,
            git_ref=git_ref,
            local_path=local_path,
            git_ssh_key=git_ssh_key,
            clear_git_ssh_key=bool(clear_git_ssh_key),
        )
    except SourceError as exc:
        db.rollback()
        return render(
            request,
            "sources/form.html",
            user=user,
            source=None,
            error=exc.message,
            form=_form_state(locals()),
            status_code=400,
        )
    return _redirect(f"/sources/{source.id}/edit", notice="Source saved.")


@router.get("/sources/{source_id}/edit")
def source_edit(
    source_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    return render(request, "sources/form.html", user=user, source=source)


@router.post("/sources/{source_id}")
def source_update(
    source_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    git_url: Annotated[str, Form()] = "",
    git_ref: Annotated[str, Form()] = "",
    local_path: Annotated[str, Form()] = "",
    git_ssh_key: Annotated[str, Form()] = "",
    clear_git_ssh_key: Annotated[str, Form()] = "",
):
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    try:
        save_source(
            db,
            source=source,
            name=name,
            git_url=git_url,
            git_ref=git_ref,
            local_path=local_path,
            git_ssh_key=git_ssh_key,
            clear_git_ssh_key=bool(clear_git_ssh_key),
        )
    except SourceError as exc:
        db.rollback()
        return render(
            request,
            "sources/form.html",
            user=user,
            source=source,
            error=exc.message,
            form=_form_state(locals()),
            status_code=400,
        )
    return _redirect(f"/sources/{source.id}/edit", notice="Source saved.")


@router.post("/sources/{source_id}/delete")
def source_delete(
    source_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    del user
    source = db.get(Source, source_id)
    if source is None:
        return _redirect("/sources", error="Source not found.")
    try:
        delete_source(db, source)
    except SourceError as exc:
        db.rollback()
        return _redirect("/sources", error=exc.message)
    return _redirect("/sources", notice="Source removed.")


@router.get("/runtimes")
def runtime_list(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_session)
):
    runtimes = list(db.scalars(select(Runtime).order_by(Runtime.name)))
    return render(
        request,
        "runtimes/list.html",
        user=user,
        runtimes=runtimes,
        usage=_usage(db, Stack.runtime_id),
    )


@router.get("/runtimes/new")
def runtime_new(request: Request, user: User = Depends(require_admin)):
    return render(request, "runtimes/form.html", user=user, runtime=None)


@router.post("/runtimes")
def runtime_create(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    executor: Annotated[str, Form()] = "local",
    secret_ref: Annotated[str, Form()] = "",
):
    try:
        runtime = save_runtime(
            db, runtime=None, name=name, executor=executor, secret_ref=secret_ref
        )
    except RuntimeConfigError as exc:
        db.rollback()
        return render(
            request,
            "runtimes/form.html",
            user=user,
            runtime=None,
            error=exc.message,
            form=_form_state(locals()),
            status_code=400,
        )
    return _redirect(f"/runtimes/{runtime.id}/edit", notice="Runtime saved.")


@router.get("/runtimes/{runtime_id}/edit")
def runtime_edit(
    runtime_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Runtime not found.")
    return render(request, "runtimes/form.html", user=user, runtime=runtime)


@router.post("/runtimes/{runtime_id}")
def runtime_update(
    runtime_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    executor: Annotated[str, Form()] = "local",
    secret_ref: Annotated[str, Form()] = "",
):
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Runtime not found.")
    try:
        save_runtime(db, runtime=runtime, name=name, executor=executor, secret_ref=secret_ref)
    except RuntimeConfigError as exc:
        db.rollback()
        return render(
            request,
            "runtimes/form.html",
            user=user,
            runtime=runtime,
            error=exc.message,
            form=_form_state(locals()),
            status_code=400,
        )
    return _redirect(f"/runtimes/{runtime.id}/edit", notice="Runtime saved.")


@router.post("/runtimes/{runtime_id}/delete")
def runtime_delete(
    runtime_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    del user
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        return _redirect("/runtimes", error="Runtime not found.")
    try:
        delete_runtime(db, runtime)
    except RuntimeConfigError as exc:
        db.rollback()
        return _redirect("/runtimes", error=exc.message)
    return _redirect("/runtimes", notice="Runtime removed.")


@router.get("/settings/users")
def users_page(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_session)
):
    return render(request, "settings/users.html", user=user, users=user_choices(db))


@router.post("/settings/users/{user_id}")
def users_update(
    user_id: uuid.UUID,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_session),
    role: Annotated[str, Form()] = "member",
    groups: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
):
    subject = db.get(User, user_id)
    if subject is None:
        return _redirect("/settings/users", error="User not found.")
    try:
        update_user(
            db,
            subject,
            actor,
            enabled=enabled == "on",
            role=role,
            groups=[part.strip() for part in groups.split(",") if part.strip()],
        )
    except AuthError as exc:
        db.rollback()
        return _redirect("/settings/users", error=exc.message)
    return _redirect("/settings/users", notice="User updated.")


@router.get("/settings/auth")
def auth_page(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_session)
):
    return render(request, "settings/auth.html", user=user, oidc=ensure_oidc(db))


@router.post("/settings/auth")
def auth_update(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    issuer: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
    scopes: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
):
    del user
    save_oidc(
        db,
        enabled=enabled == "on",
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    return _redirect("/settings/auth", notice="OIDC settings saved.")


@router.get("/settings/integrations")
def integrations_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    return render(
        request, "settings/integrations.html", user=user, integrations=list_integrations(db)
    )


@router.post("/settings/integrations")
def integrations_create(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    secret: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
):
    del user
    if not name.strip() or not url.strip():
        return _redirect("/settings/integrations", error="Name and URL are required.")
    save_webhook(db, name=name, url=url, secret=secret, enabled=enabled == "on")
    return _redirect("/settings/integrations", notice="Webhook saved.")


@router.post("/settings/integrations/{integration_id}/delete")
def integrations_delete(
    integration_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    del user
    row = db.get(Integration, integration_id)
    if row is not None:
        db.delete(row)
    return _redirect("/settings/integrations", notice="Webhook removed.")


def _stack_or_404(db: Session, stack_id: uuid.UUID) -> Stack:
    stack = db.scalar(
        select(Stack)
        .where(Stack.id == stack_id)
        .options(*load_stack(), selectinload(Stack.approvers))
    )
    if stack is None:
        raise HTTPException(status_code=404, detail="Stack not found.")
    return stack


def _run_or_404(db: Session, run_id: uuid.UUID) -> Run:
    run = db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .options(
            load_run_stack(selectinload(Stack.approvers)),
            selectinload(Run.logs),
            selectinload(Run.executions),
            selectinload(Run.trigger_user),
            selectinload(Run.approved_by),
            selectinload(Run.rejected_by),
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


def _stack_form_context(
    db: Session,
    stack: Stack | None,
    *,
    selected_users: set[str] | None = None,
    group_text: str | None = None,
    source_id: str = "",
) -> dict:
    sources = list(db.scalars(select(Source).order_by(Source.name)))
    runtimes = list(db.scalars(select(Runtime).order_by(Runtime.name)))
    if stack is not None and not source_id:
        source_id = str(stack.source_id)
    elif not source_id and sources:
        source_id = str(sources[0].id)
    if selected_users is None:
        selected_users = (
            {str(row.user_id) for row in stack.approvers if row.user_id}
            if stack is not None
            else set()
        )
    if group_text is None:
        group_text = (
            ", ".join(row.group_name for row in stack.approvers if row.group_name)
            if stack is not None
            else ""
        )
    return {
        "users": user_choices(db),
        "sources": sources,
        "runtimes": runtimes,
        "ref_hint": _ref_hint(sources, source_id),
        "selected_users": selected_users,
        "group_text": group_text,
    }


def _ref_hint(sources: list[Source], source_id: str) -> str:
    chosen = next((item for item in sources if str(item.id) == source_id), None)
    if chosen is not None and chosen.git_url and chosen.git_ref:
        return f"Leave blank to use {chosen.git_ref}."
    if chosen is not None and chosen.local_path and not chosen.git_url:
        return "This source is a local path, so a git ref does not apply."
    return "Leave blank to use the source ref."


def _usage(db: Session, column) -> dict:
    return dict(db.execute(select(column, func.count()).group_by(column)).all())


def _form_state(values: dict) -> dict:
    keys = [
        "name",
        "deploy_file",
        "inventory",
        "default_limit",
        "source_id",
        "runtime_id",
        "git_ref",
        "git_url",
        "local_path",
        "executor",
        "secret_ref",
        "schedule_cron",
    ]
    return {key: values.get(key, "") for key in keys}
