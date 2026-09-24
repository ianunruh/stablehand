from __future__ import annotations


def normalize_limit(value: str | None) -> str | None:
    if value is None:
        return None
    patterns = [pattern.strip() for pattern in value.split(",") if pattern.strip()]
    return ", ".join(patterns) or None


def limit_patterns(value: str | None) -> list[str]:
    normalized = normalize_limit(value)
    return normalized.split(", ") if normalized else []
