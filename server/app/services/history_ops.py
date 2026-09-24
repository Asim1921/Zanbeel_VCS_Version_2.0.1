"""Cherry-pick, revert and rebase.

All three are three-way merges with the corners assigned differently, which is why they
share an implementation:

    cherry-pick C onto B   base = tree(parent(C))   ours = tree(head B)   theirs = tree(C)
    revert C on B          base = tree(C)           ours = tree(head B)   theirs = tree(parent(C))
    rebase B onto A        the cherry-pick assignment, applied to each of B's own commits
                           in turn, with `ours` carried forward from the previous step

Cherry-pick replays the change C introduced; revert runs the same change backwards. In
both cases "ours" is the branch being written to, so unrelated work on that branch is
preserved and only genuine overlaps come back as conflicts.

Cherry-pick and revert never rewrite history: each adds a commit on top of the target, so
a revert is itself revertible and nothing already pushed is mutated. Rebase does move a
branch, so it simulates the entire sequence first and refuses as a whole if any step
conflicts -- there is no interactive "continue" here to rescue a half-finished rebase, and
it reports the previous tip so the old line of development can still be recovered.
"""

import hashlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database.crud import BranchCRUD, CommitCRUD, FileObjectCRUD, UserCRUD
from database.models import Commit

from app.services.commit_graph import _get_commit_tree
from app.services.merge import _merge_trees
from app.services import refs
from app.services.signing import sign_commit


class HistoryOpError(Exception):
    """Raised with a message suitable for returning to the caller."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _parents_of(db: Session, commit: Commit) -> List[str]:
    parents: List[str] = []
    if commit.parent_commit_id:
        parents.append(commit.parent_commit_id)
    for link in sorted(commit.parent_links or [], key=lambda l: getattr(l, "parent_order", 0)):
        if link.parent_commit_id and link.parent_commit_id not in parents:
            parents.append(link.parent_commit_id)
    return parents


def _load_commit(db: Session, repo_id: str, commit_id: str) -> Commit:
    commit = (
        db.query(Commit)
        .filter(Commit.id == commit_id, Commit.repository_id == repo_id)
        .first()
    )
    if not commit:
        raise HistoryOpError(f"Commit {commit_id[:12]} not found in this repository", 404)
    return commit


def _resolve_source_parent(db: Session, commit: Commit, mainline: Optional[int]) -> Optional[str]:
    """Which parent to treat as 'before' for this commit.

    A merge commit has more than one, and the answer changes what the operation means, so
    it must be chosen explicitly rather than guessed.
    """
    parents = _parents_of(db, commit)
    if len(parents) <= 1:
        if mainline not in (None, 1):
            raise HistoryOpError(
                f"Commit {commit.id[:12]} is not a merge; drop the mainline argument.", 400
            )
        return parents[0] if parents else None

    if mainline is None:
        raise HistoryOpError(
            f"Commit {commit.id[:12]} is a merge with {len(parents)} parents. "
            f"Pass mainline=1..{len(parents)} to say which side to treat as the original.",
            400,
        )
    if not 1 <= mainline <= len(parents):
        raise HistoryOpError(
            f"mainline must be between 1 and {len(parents)} for this merge commit.", 400
        )
    return parents[mainline - 1]


def _plan(
    db: Session,
    repo_id: str,
    commit_id: str,
    branch_name: str,
    operation: str,
    mainline: Optional[int] = None,
) -> Dict[str, Any]:
    """Compute the resulting tree for a cherry-pick or revert without writing anything."""
    branch = BranchCRUD.get_branch(db, repo_id, branch_name)
    if not branch:
        raise HistoryOpError(f"Branch '{branch_name}' not found", 404)

    source = _load_commit(db, repo_id, commit_id)
    parent_id = _resolve_source_parent(db, source, mainline)

    source_tree = _get_commit_tree(db, source.id)
    parent_tree = _get_commit_tree(db, parent_id) if parent_id else {}
    ours = _get_commit_tree(db, branch.head_commit_id)

    if operation == "cherry-pick":
        base, theirs = parent_tree, source_tree
    elif operation == "revert":
        base, theirs = source_tree, parent_tree
    else:                                            # pragma: no cover - guarded by callers
        raise HistoryOpError(f"unknown operation {operation!r}")

    merged, conflicts = _merge_trees(base, ours, theirs)

    return {
        "branch": branch,
        "source": source,
        "source_parent_id": parent_id,
        "tree": merged,
        "conflicts": sorted(conflicts),
        "unchanged": merged == ours,
    }


def _write_commit(
    db: Session,
    repo_id: str,
    branch,
    tree: Dict[str, bytes],
    author_username: str,
    message: str,
    seed: str,
) -> Commit:
    commit_id = hashlib.sha256(
        f"{seed}:{repo_id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]

    entries = []
    for path, content in tree.items():
        stored = FileObjectCRUD.store_file_object(db, content)
        entries.append(
            SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content))
        )

    commit_data = {
        "id": commit_id,
        "repository_id": repo_id,
        "author": author_username,
        "message": message,
        # A single parent: the branch tip. The picked/reverted commit is referenced in the
        # message, not the graph, so history stays linear and the operation is repeatable.
        "parents": [branch.head_commit_id] if branch.head_commit_id else [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    commit = CommitCRUD.create_commit_from_file_hashes(db, commit_data, entries)
    refs.update_reference_by_id(
        db, repository_id=repo_id, actor_username=author_username,
        branch_name=branch.name, new_commit_id=commit.id,
    )
    # Attest it like a pushed commit, otherwise every cherry-pick, revert and rebase
    # leaves a permanently unsigned gap in the history verification covers. The user
    # running the operation is both the author and the pusher here.
    sign_commit(db, commit, author_username, commit.author_id)
    return commit


def _subject(commit: Commit) -> str:
    message = commit.message or ""
    return message.splitlines()[0] if message else commit.id[:12]


def cherry_pick(
    db: Session,
    repo_id: str,
    commit_id: str,
    branch_name: str,
    author_username: str,
    mainline: Optional[int] = None,
    message: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Replay the change introduced by `commit_id` on top of `branch_name`."""
    plan = _plan(db, repo_id, commit_id, branch_name, "cherry-pick", mainline)
    source = plan["source"]

    if plan["conflicts"]:
        return {"status": "conflicts", "conflicts": plan["conflicts"],
                "source_commit_id": source.id, "branch": branch_name}
    if plan["unchanged"]:
        return {"status": "no-op", "reason": "That change is already present on this branch.",
                "source_commit_id": source.id, "branch": branch_name}
    if dry_run:
        return {"status": "clean", "source_commit_id": source.id, "branch": branch_name,
                "files": len(plan["tree"])}

    text = message or f"Cherry-pick '{_subject(source)}'\n\n(cherry picked from commit {source.id})"
    commit = _write_commit(db, repo_id, plan["branch"], plan["tree"],
                           author_username, text, f"cherry-pick:{source.id}")
    return {"status": "applied", "commit_id": commit.id,
            "source_commit_id": source.id, "branch": branch_name}


def revert(
    db: Session,
    repo_id: str,
    commit_id: str,
    branch_name: str,
    author_username: str,
    mainline: Optional[int] = None,
    message: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create a commit undoing `commit_id`, on top of `branch_name`."""
    plan = _plan(db, repo_id, commit_id, branch_name, "revert", mainline)
    source = plan["source"]

    if plan["conflicts"]:
        return {"status": "conflicts", "conflicts": plan["conflicts"],
                "source_commit_id": source.id, "branch": branch_name}
    if plan["unchanged"]:
        return {"status": "no-op", "reason": "That change is not present on this branch.",
                "source_commit_id": source.id, "branch": branch_name}
    if dry_run:
        return {"status": "clean", "source_commit_id": source.id, "branch": branch_name,
                "files": len(plan["tree"])}

    text = message or f"Revert '{_subject(source)}'\n\nThis reverts commit {source.id}."
    commit = _write_commit(db, repo_id, plan["branch"], plan["tree"],
                           author_username, text, f"revert:{source.id}")
    return {"status": "reverted", "commit_id": commit.id,
            "source_commit_id": source.id, "branch": branch_name}


def _replay_order(db: Session, head_id: str, base_id: Optional[str]) -> List[str]:
    """Commits from `base_id` (exclusive) up to `head_id`, oldest first.

    Follows first parents, which is the sequence a rebase replays. Stops at the merge base
    so only the branch's own work is carried over.
    """
    chain: List[str] = []
    cursor: Optional[str] = head_id
    seen = set()
    while cursor and cursor != base_id and cursor not in seen:
        seen.add(cursor)
        chain.append(cursor)
        commit = db.query(Commit).filter(Commit.id == cursor).first()
        if not commit:
            break
        parents = _parents_of(db, commit)
        cursor = parents[0] if parents else None
    chain.reverse()
    return chain


def rebase(
    db: Session,
    repo_id: str,
    branch_name: str,
    onto_branch: str,
    author_username: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Replay a branch's own commits on top of another branch.

    All-or-nothing: the whole sequence is simulated first, and a conflict at any step
    aborts before anything is written. That matters because a rebase that fails half way
    leaves a branch in a state no one asked for -- there is no interactive "continue" here
    to rescue it.
    """
    from app.services.commit_graph import _find_merge_base

    branch = BranchCRUD.get_branch(db, repo_id, branch_name)
    if not branch:
        raise HistoryOpError(f"Branch '{branch_name}' not found", 404)
    onto = BranchCRUD.get_branch(db, repo_id, onto_branch)
    if not onto:
        raise HistoryOpError(f"Branch '{onto_branch}' not found", 404)
    if branch.name == onto.name:
        raise HistoryOpError("Cannot rebase a branch onto itself", 400)
    if not branch.head_commit_id:
        raise HistoryOpError(f"Branch '{branch_name}' has no commits", 400)

    base_id = _find_merge_base(db, branch.head_commit_id, onto.head_commit_id)
    if base_id == branch.head_commit_id:
        return {"status": "no-op", "reason": f"'{branch_name}' is already contained in "
                                             f"'{onto_branch}'.", "replayed": 0}
    if base_id == onto.head_commit_id:
        return {"status": "no-op", "reason": f"'{branch_name}' is already on top of "
                                             f"'{onto_branch}'.", "replayed": 0}

    to_replay = _replay_order(db, branch.head_commit_id, base_id)
    if not to_replay:
        return {"status": "no-op", "reason": "Nothing to replay.", "replayed": 0}

    # Simulate the whole sequence before writing anything.
    cursor_tree = _get_commit_tree(db, onto.head_commit_id)
    planned: List[Tuple[Commit, Dict[str, bytes]]] = []
    for commit_id in to_replay:
        commit = _load_commit(db, repo_id, commit_id)
        parents = _parents_of(db, commit)
        parent_tree = _get_commit_tree(db, parents[0]) if parents else {}
        merged, conflicts = _merge_trees(parent_tree, cursor_tree, _get_commit_tree(db, commit_id))
        if conflicts:
            return {
                "status": "conflicts",
                "conflicts": sorted(conflicts),
                "failed_at": commit_id,
                "failed_subject": _subject(commit),
                "replayed": 0,
                "branch": branch_name,
                "onto": onto_branch,
            }
        cursor_tree = merged
        planned.append((commit, merged))

    if dry_run:
        return {"status": "clean", "replayed": len(planned), "branch": branch_name,
                "onto": onto_branch,
                "commits": [{"commit_id": c.id, "subject": _subject(c)} for c, _ in planned]}

    original_head = branch.head_commit_id
    parent_id = onto.head_commit_id
    new_ids: List[str] = []
    # Replayed commits keep their original author, so the pusher is the user running the
    # rebase -- resolved once rather than per replayed commit.
    actor = UserCRUD.get_user_by_username(db, author_username)
    actor_id = actor.id if actor else None
    for commit, tree in planned:
        entries = []
        for path, content in tree.items():
            stored = FileObjectCRUD.store_file_object(db, content)
            entries.append(
                SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content))
            )
        new_id = hashlib.sha256(
            f"rebase:{commit.id}:{parent_id}:{datetime.utcnow().isoformat()}".encode()
        ).hexdigest()[:40]
        written = CommitCRUD.create_commit_from_file_hashes(
            db,
            {
                "id": new_id,
                "repository_id": repo_id,
                # Authorship follows the original commit; the rebase is not new work.
                "author": commit.author.username if commit.author else author_username,
                "message": commit.message,
                "parents": [parent_id] if parent_id else [],
                "timestamp": datetime.utcnow().isoformat(),
            },
            entries,
        )
        sign_commit(db, written, author_username, actor_id)
        parent_id = written.id
        new_ids.append(written.id)

    # A rebase replaces the tip with commits that are not its descendants, so the
    # reference service classifies this as a force update and a protected branch
    # refuses it -- which is the point.
    refs.update_reference_by_id(
        db, repository_id=repo_id, actor_username=author_username,
        branch_name=branch.name, new_commit_id=parent_id,
    )
    return {
        "status": "rebased",
        "branch": branch_name,
        "onto": onto_branch,
        "replayed": len(new_ids),
        "new_head": parent_id,
        # The pre-rebase tip, so the old line of development can still be recovered.
        "previous_head": original_head,
        "new_commit_ids": new_ids,
    }
