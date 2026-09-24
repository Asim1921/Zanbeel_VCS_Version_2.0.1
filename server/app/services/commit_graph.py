"""Commit DAG traversal: parents, reachability, ancestry and merge bases."""

from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import Commit, CommitParent
from app.services.paths import normalize


def _get_commit_parents(db: Session, commit_id: Optional[str]) -> List[str]:
    if not commit_id:
        return []
    links = db.query(CommitParent).filter(CommitParent.commit_id == commit_id).order_by(CommitParent.parent_order).all()
    if links:
        return [link.parent_commit_id for link in links]
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if commit and commit.parent_commit_id:
        return [commit.parent_commit_id]
    return []

def _collect_reachable_commits(db: Session, head_commit_id: Optional[str], stop_at: Optional[str] = None) -> List[str]:
    if not head_commit_id:
        return []
    visited = set()
    stack = [head_commit_id]
    while stack:
        current = stack.pop()
        if not current or current in visited:
            continue
        visited.add(current)
        if stop_at and current == stop_at:
            continue
        parents = _get_commit_parents(db, current)
        for parent_id in parents:
            if parent_id and parent_id not in visited:
                stack.append(parent_id)
    return list(visited)

def _is_ancestor(db: Session, ancestor_id: str, descendant_id: str) -> bool:
    if not ancestor_id or not descendant_id:
        return False
    if ancestor_id == descendant_id:
        return True
    visited = set()
    stack = [descendant_id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for parent_id in _get_commit_parents(db, current):
            if parent_id == ancestor_id:
                return True
            if parent_id and parent_id not in visited:
                stack.append(parent_id)
    return False


def _get_commit_tree(db: Session, commit_id: Optional[str]) -> Dict[str, bytes]:
    if not commit_id:
        return {}
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return {}
    # Keyed by the canonical path, not the stored one. A commit can hold the same
    # logical file under two spellings -- a client that sends forward slashes writes
    # the changed file as "a/b.py" while the unchanged copy carried over from the
    # parent is still "a\b.py". Keying raw made those two separate entries, so a
    # modified file compared as one "added" plus one "removed" and produced no diff.
    tree = {}
    canonical_source = {}
    for commit_file in commit.files:
        if not commit_file.file_object:
            continue
        raw = commit_file.file_path
        key = normalize(raw)
        # On collision keep the canonically spelled row: it is the one the newer
        # client wrote, so it carries the current content.
        if key in tree and canonical_source.get(key) and raw != key:
            continue
        tree[key] = commit_file.file_object.content
        canonical_source[key] = raw == key
    return tree

def _get_ancestors_with_depth(db: Session, commit_id: Optional[str]) -> Dict[str, int]:
    if not commit_id:
        return {}
    depth = {commit_id: 0}
    queue = [(commit_id, 0)]
    while queue:
        current, dist = queue.pop(0)
        for parent_id in _get_commit_parents(db, current):
            if parent_id and parent_id not in depth:
                depth[parent_id] = dist + 1
                queue.append((parent_id, dist + 1))
    return depth

def _find_merge_base(db: Session, commit_a: Optional[str], commit_b: Optional[str]) -> Optional[str]:
    if not commit_a or not commit_b:
        return None
    depth_a = _get_ancestors_with_depth(db, commit_a)
    depth_b = _get_ancestors_with_depth(db, commit_b)
    common = set(depth_a.keys()) & set(depth_b.keys())
    if not common:
        return None
    return min(common, key=lambda cid: depth_a[cid] + depth_b[cid])
