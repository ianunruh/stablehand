from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from stablehand.auth.service import SESSION_COOKIE, ensure_oidc
from stablehand.config import get_settings
from stablehand.db import session_scope
from stablehand.models import Role, Stack, User
from stablehand.runs.machine import router as machine_router
from stablehand.runtimes.service import save_runtime
from stablehand.security import csrf_matches, hash_password
from stablehand.sources.service import save_source
from stablehand.stacks.service import save_stack
from stablehand.ui.render import render
from stablehand.ui.routes import Forbidden, LoginRequired
from stablehand.ui.routes import router as ui_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    bootstrap()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Stablehand", lifespan=lifespan)
    static_dir = settings.package_dir / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        if request.method in {"GET", "HEAD", "OPTIONS"} or request.url.path.startswith("/api/"):
            return await call_next(request)
        if request.url.path in {"/login", "/healthz"}:
            return await call_next(request)
        session_id = request.cookies.get(SESSION_COOKIE, "")
        if not session_id:
            return await call_next(request)
        provided = request.headers.get("x-csrf-token", "")
        if not csrf_matches(session_id, provided):
            return HTMLResponse("CSRF check failed", status_code=403)
        return await call_next(request)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        del exc
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        del exc
        return render(request, "forbidden.html", status_code=403)

    @app.exception_handler(404)
    async def missing(request: Request, exc: HTTPException):
        del exc
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return render(request, "not_found.html", status_code=404)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    app.include_router(ui_router)
    app.include_router(machine_router)
    return app


def bootstrap() -> None:
    settings = get_settings()
    with session_scope() as db:
        user_count = int(db.scalar(select(func.count()).select_from(User)) or 0)
        if user_count == 0 and settings.admin_email and settings.admin_password:
            db.add(
                User(
                    email=settings.admin_email.strip().lower(),
                    name="Admin",
                    password_hash=hash_password(settings.admin_password),
                    role=Role.admin.value,
                    groups=[],
                    enabled=True,
                )
            )
            db.flush()
        ensure_oidc(db)
        stack_count = int(db.scalar(select(func.count()).select_from(Stack)) or 0)
        if settings.seed_demo and stack_count == 0:
            demo = settings.demo_path or str(
                Path(__file__).resolve().parents[2] / "examples" / "demo"
            )
            source = save_source(
                db,
                source=None,
                name="Demo",
                git_url="",
                git_ref="",
                local_path=demo,
            )
            runtime = save_runtime(
                db,
                runtime=None,
                name="Local",
                executor="local",
                secret_ref="",
            )
            save_stack(
                db,
                stack=None,
                name="Demo",
                deploy_file="deploy.py",
                inventory="inventory.py",
                default_limit="",
                source_id=str(source.id),
                runtime_id=str(runtime.id),
                git_ref="",
                schedule_cron="",
                approver_user_ids=[],
                approver_groups="",
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run("stablehand.web:app", host="0.0.0.0", port=8000, factory=False)


app = create_app()
