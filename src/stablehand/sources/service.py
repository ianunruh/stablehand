from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stablehand.gitssh import is_ssh_git_url
from stablehand.models import Source, Stack
from stablehand.security import encrypt_secret


class SourceError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def save_source(
    session: Session,
    *,
    source: Source | None,
    name: str,
    git_url: str,
    git_ref: str,
    local_path: str,
    git_ssh_key: str = "",
    clear_git_ssh_key: bool = False,
) -> Source:
    cleaned_name = name.strip()
    cleaned_url = git_url.strip()
    cleaned_path = local_path.strip()
    if not cleaned_name:
        raise SourceError("Name is required.")
    if cleaned_url and cleaned_path:
        raise SourceError("Provide a git URL or a local path, not both.")
    if not cleaned_url and not cleaned_path:
        raise SourceError("Provide a git URL or a local path.")
    encrypted_key = _git_ssh_key(source, git_ssh_key, clear_git_ssh_key)
    if encrypted_key and not is_ssh_git_url(cleaned_url):
        raise SourceError(
            "A deploy key requires an SSH git URL, such as git@github.com:org/repo.git."
        )
    existing = session.scalar(select(Source).where(Source.name == cleaned_name))
    if existing is not None and (source is None or existing.id != source.id):
        raise SourceError("A source with this name already exists.")
    if source is None:
        source = Source(name=cleaned_name)
        session.add(source)
    source.name = cleaned_name
    source.git_url = cleaned_url or None
    source.local_path = cleaned_path or None
    source.git_ref = (git_ref.strip() or "main") if source.git_url else None
    source.git_ssh_key_encrypted = encrypted_key
    session.flush()
    session.refresh(source)
    return source


def delete_source(session: Session, source: Source) -> None:
    count = int(
        session.scalar(select(func.count()).select_from(Stack).where(Stack.source_id == source.id))
        or 0
    )
    if count:
        noun = "stack" if count == 1 else "stacks"
        raise SourceError(f"{count} {noun} use this source.")
    session.delete(source)
    try:
        session.flush()
    except IntegrityError as exc:
        raise SourceError("Stacks still use this source.") from exc


def get_source(session: Session, source_id: uuid.UUID) -> Source | None:
    return session.get(Source, source_id)


def _git_ssh_key(source: Source | None, pasted: str, clear: bool) -> str:
    text = pasted.strip()
    if text:
        return encrypt_secret(_normalize_deploy_key(text))
    if clear or source is None:
        return ""
    return source.git_ssh_key_encrypted or ""


def _normalize_deploy_key(value: str) -> str:
    if "-----BEGIN " not in value or "PRIVATE KEY" not in value:
        raise SourceError("Deploy key must be a PEM SSH private key.")
    if len(value) > 100_000:
        raise SourceError("Deploy key is too large.")
    return value + "\n"
