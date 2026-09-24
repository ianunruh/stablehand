import os

os.environ.setdefault("STABLEHAND_DATABASE_URL", "sqlite://")
os.environ.setdefault("STABLEHAND_SECRET_KEY", "test-secret-key")
os.environ.setdefault("STABLEHAND_ADMIN_EMAIL", "")
os.environ.setdefault("STABLEHAND_ADMIN_PASSWORD", "")
os.environ.setdefault("STABLEHAND_SEED_DEMO", "false")
os.environ.setdefault("STABLEHAND_PUBLIC_URL", "http://testserver")
os.environ.setdefault("STABLEHAND_RUNNER_API_URL", "http://testserver")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from stablehand.config import get_settings
from stablehand.db import get_engine, get_sessionmaker, reset_engine
from stablehand.models import Base, Role, Runtime, Source, Stack, User
from stablehand.security import csrf_token, hash_password
from stablehand.web import create_app

get_settings.cache_clear()


@pytest.fixture
def database():
    get_settings.cache_clear()
    reset_engine()
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    reset_engine()


@pytest.fixture
def db(database) -> Session:
    del database
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(database) -> TestClient:
    del database
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


def make_stack(
    *,
    name: str = "demo",
    deploy_file: str = "deploy.py",
    inventory: str = "inventory.py",
    local_path: str | None = "/tmp/demo",
    git_url: str | None = None,
    git_ref: str | None = None,
    stack_ref: str | None = None,
    git_ssh_key_encrypted: str = "",
    executor: str = "local",
    secret_ref: str | None = None,
    source_name: str | None = None,
    runtime_name: str | None = None,
    **stack_fields,
) -> Stack:
    if git_url:
        source = Source(
            name=source_name or f"{name} source",
            git_url=git_url,
            git_ref=git_ref or "main",
            local_path=None,
            git_ssh_key_encrypted=git_ssh_key_encrypted,
        )
    else:
        source = Source(
            name=source_name or f"{name} source",
            git_url=None,
            git_ref=None,
            local_path=local_path,
            git_ssh_key_encrypted=git_ssh_key_encrypted,
        )
    runtime = Runtime(
        name=runtime_name or f"{name} runtime",
        executor=executor,
        secret_ref=secret_ref,
    )
    return Stack(
        name=name,
        deploy_file=deploy_file,
        inventory=inventory,
        source=source,
        runtime=runtime,
        git_ref=stack_ref,
        **stack_fields,
    )


def add_user(
    db: Session, email: str, role: str = Role.member.value, password: str = "password123"
) -> User:
    user = User(
        email=email,
        name=email.split("@", 1)[0],
        password_hash=hash_password(password),
        role=role,
        groups=[],
        enabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(client: TestClient, email: str, password: str = "password123") -> str:
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf_token(client.cookies["stablehand_session"])


def auth(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}
