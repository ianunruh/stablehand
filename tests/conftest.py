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
from stablehand.models import Base, Role, User
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
