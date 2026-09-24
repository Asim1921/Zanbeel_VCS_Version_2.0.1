r"""Repository file paths: one spelling in, both spellings matched.

Paths are stored exactly as the pushing client sent them, so a Windows client writes
``server\app\config.py`` while a POSIX one writes ``server/app/config.py``. The same
logical file therefore has two possible spellings in the database, and a lookup that
compares raw strings finds only one of them.

They cannot be rewritten in place: ``file_path`` is part of the commit attestation digest
(see app/services/signing.py), so normalising stored rows would make every existing commit
verify as tampered. Queries reconcile the two forms instead, which is what these helpers
are for. New pushes send forward slashes, so this is a bridge for existing history rather
than a permanent tax.
"""

from typing import Iterable, List, Set

BACKSLASH = chr(92)


def normalize(path: str) -> str:
    """Canonical form: forward slashes, no leading ``./``."""
    text = (path or "").replace(BACKSLASH, "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def variants(path: str) -> List[str]:
    """Both spellings of ``path``, for a SQL ``IN`` against stored values.

    A list rather than a set so the generated query is stable and testable.
    """
    canonical = normalize(path)
    if not canonical:
        return []
    windows = canonical.replace("/", BACKSLASH)
    return [canonical] if windows == canonical else [canonical, windows]


def variants_of_many(paths: Iterable[str]) -> List[str]:
    """Both spellings of every path, de-duplicated, order preserved."""
    seen: Set[str] = set()
    out: List[str] = []
    for path in paths:
        for candidate in variants(path):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def same_path(left: str, right: str) -> bool:
    """Whether two paths name the same file regardless of separator."""
    return normalize(left) == normalize(right)
