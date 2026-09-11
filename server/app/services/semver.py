"""Semantic version validation for releases."""

from fastapi import HTTPException

from app.config import SEMVER_RE


def _validate_semver(version: str) -> str:
    normalized = (version or "").strip()
    if not SEMVER_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Version must follow semantic versioning (e.g., 1.2.3 or v1.2.3)")
    return normalized
