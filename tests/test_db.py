import pytest
from sqlalchemy import select

from stablehand.db import get_session, get_sessionmaker
from stablehand.models import User


def test_request_session_commits_on_success(database):
    del database
    dependency = get_session()
    session = next(dependency)
    session.add(User(email="commit@example.com", name="Commit"))
    with pytest.raises(StopIteration):
        next(dependency)

    verification = get_sessionmaker()()
    try:
        assert (
            verification.scalar(select(User).where(User.email == "commit@example.com")) is not None
        )
    finally:
        verification.close()


def test_request_session_rolls_back_on_exception(database):
    del database
    dependency = get_session()
    session = next(dependency)
    session.add(User(email="rollback@example.com", name="Rollback"))
    with pytest.raises(RuntimeError, match="boom"):
        dependency.throw(RuntimeError("boom"))

    verification = get_sessionmaker()()
    try:
        assert verification.scalar(select(User).where(User.email == "rollback@example.com")) is None
    finally:
        verification.close()
