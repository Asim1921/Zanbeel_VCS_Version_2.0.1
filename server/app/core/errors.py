"""Shared HTTP error helpers."""

from typing import Optional

from fastapi import HTTPException


def _raise_head_mismatch(branch_name: str, expected_head: Optional[str], actual_head: Optional[str]):
    raise HTTPException(
        status_code=409,
        detail={
            "code": "HEAD_MISMATCH",
            "branch": branch_name,
            "expected_head": expected_head,
            "actual_head": actual_head,
            "message": "Branch head changed. Refresh and retry."
        }
    )
