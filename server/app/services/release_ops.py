"""Tags and releases: creating, describing, editing and removing them.

A release is a tag with a version, a title and notes. Before this module the routes
committed the tag and the release in two transactions (a refused release left its new
tag behind), let one tag carry several releases, tagged ``repository.head_commit_id``
(which moves on writes that never touch a branch) rather than the default branch, and
could delete a tag out from under the release built on it. Releases could be neither
corrected nor withdrawn.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.models import Commit, PullRequest, Release, Repository, Tag, User

from app.services import repo_refs
from app.services.semver import _validate_semver


# --- versions -----------------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _prerelease_key(text: str) -> Tuple:
    """Semantic-versioning precedence for the part after '-': numeric identifiers
    compare as numbers and sort before alphanumeric ones."""
    parts = []
    for item in text.split("."):
        parts.append((0, int(item), "") if item.isdigit() else (1, 0, item))
    return tuple(parts)


def version_key(version: str) -> Tuple:
    """Sort key giving semantic-version order; anything unparsable sorts first."""
    match = _VERSION.match((version or "").strip())
    if not match:
        return (-1, -1, -1, 0, ())
    major, minor, patch, pre = match.groups()
    # A release outranks any of its own pre-releases: 1.0.0 > 1.0.0-rc.1.
    return (int(major), int(minor), int(patch), 0 if pre else 1, _prerelease_key(pre) if pre else ())


def is_prerelease(version: str) -> bool:
    match = _VERSION.match((version or "").strip())
    return bool(match and match.group(4))


# --- shapes -------------------------------------------------------------------------

def tag_to_dict(tag: Tag, release: Optional[Release] = None) -> Dict[str, Any]:
    return {
        "name": tag.name,
        "commit_id": tag.commit_id,
        "message": tag.message,
        "created_at": tag.created_at.isoformat() if tag.created_at else None,
        "created_by": tag.created_by.username if tag.created_by else None,
        "commit": repo_refs.commit_summary(tag.commit),
        "release": release.version if release is not None else None,
    }


def release_to_dict(release: Release, latest_version: Optional[str] = None) -> Dict[str, Any]:
    tag = release.tag
    return {
        "version": release.version,
        "title": release.title,
        "notes": release.notes,
        "tag": tag.name if tag else None,
        "commit_id": tag.commit_id if tag else None,
        "commit": repo_refs.commit_summary(tag.commit) if tag else None,
        "created_at": release.created_at.isoformat() if release.created_at else None,
        "created_by": release.created_by.username if release.created_by else None,
        "prerelease": is_prerelease(release.version),
        "latest": latest_version is not None and release.version == latest_version,
    }


def latest_version(releases: List[Release]) -> Optional[str]:
    """The highest version that is not a pre-release -- what "latest" means on GitHub."""
    stable = [r.version for r in releases if not is_prerelease(r.version)]
    return max(stable, key=version_key) if stable else None


def list_tags(db: Session, repository_id: str, limit: Optional[int] = None,
              offset: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Newest first, one page. Returns (tags, pagination)."""
    from app.core.pagination import paginate_query

    query = db.query(Tag).filter(Tag.repository_id == repository_id).order_by(
        Tag.created_at.desc(), Tag.id.desc())
    tags, meta = paginate_query(query, limit, offset)
    page_ids = [t.id for t in tags]
    by_tag = {
        r.tag_id: r for r in
        db.query(Release).filter(Release.repository_id == repository_id,
                                 Release.tag_id.in_(page_ids)).all()
    } if page_ids else {}
    return [tag_to_dict(t, by_tag.get(t.id)) for t in tags], meta


def list_releases(db: Session, repository_id: str, limit: Optional[int] = None,
                  offset: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Newest first, one page. ``latest`` is decided across every release, not the page,
    so page two never claims a release is latest because the real one is on page one."""
    from app.core.pagination import paginate_list

    rows = (
        db.query(Release)
        .filter(Release.repository_id == repository_id)
        .order_by(Release.created_at.desc(), Release.id.desc())
        .all()
    )
    newest = latest_version(rows)
    page, meta = paginate_list(rows, limit, offset)
    return [release_to_dict(r, newest) for r in page], meta


# --- tags ---------------------------------------------------------------------------

def _target_commit(db: Session, repository: Repository, *, commit_id: Optional[str],
                   target: Optional[str]) -> str:
    if commit_id:
        return repo_refs.require_commit(db, repository, commit_id).id
    return repo_refs.resolve(db, repository, target)["commit_id"]


def create_tag(db: Session, repository: Repository, actor: User, *, name: str,
               commit_id: Optional[str] = None, target: Optional[str] = None,
               message: Optional[str] = None, commit: bool = True) -> Tag:
    tag_name = repo_refs.normalize_tag_name(name)
    if db.query(Tag).filter(Tag.repository_id == repository.id, Tag.name == tag_name).first():
        raise HTTPException(status_code=409, detail=f"Tag '{tag_name}' already exists")
    resolved = _target_commit(db, repository, commit_id=commit_id, target=target)
    tag = Tag(repository_id=repository.id, name=tag_name, commit_id=resolved,
              message=(message or "").strip() or None, created_by_id=actor.id)
    db.add(tag)
    db.flush()
    if commit:
        db.commit()
        db.refresh(tag)
    return tag


def delete_tag(db: Session, repository: Repository, name: str) -> Dict[str, Any]:
    tag = db.query(Tag).filter(Tag.repository_id == repository.id, Tag.name == name).first()
    if tag is None:
        raise HTTPException(status_code=404, detail=f"Tag '{name}' not found")
    release = db.query(Release).filter(Release.tag_id == tag.id).first()
    if release is not None:
        # The release would be left pointing at nothing: no commit, no source archive.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TAG_IN_USE",
                "message": f"Release {release.version} is built on '{name}'. "
                           "Delete the release first.",
                "release": release.version,
            },
        )
    snapshot = {"name": tag.name, "commit_id": tag.commit_id}
    db.delete(tag)
    db.commit()
    return snapshot


# --- releases -----------------------------------------------------------------------

def get_release(db: Session, repository_id: str, version: str) -> Release:
    release = (
        db.query(Release)
        .filter(Release.repository_id == repository_id, Release.version == version.strip())
        .first()
    )
    if release is None:
        raise HTTPException(status_code=404, detail=f"Release {version} not found")
    return release


def create_release(db: Session, repository: Repository, actor: User, *, version: str,
                   tag: Optional[str] = None, target: Optional[str] = None,
                   title: Optional[str] = None, notes: Optional[str] = None) -> Release:
    """Publish a release on an existing tag, or on a new tag at ``target``.

    The tag and the release are written in one transaction: a refusal leaves neither.
    """
    version = _validate_semver(version)
    if db.query(Release).filter(Release.repository_id == repository.id,
                                Release.version == version).first():
        raise HTTPException(status_code=409, detail=f"Release {version} already exists")

    tag_name = repo_refs.normalize_tag_name(tag or (version if version.startswith("v") else f"v{version}"))
    existing = db.query(Tag).filter(Tag.repository_id == repository.id, Tag.name == tag_name).first()
    if existing is not None:
        if target:
            pointed = repo_refs.resolve(db, repository, target)["commit_id"]
            if pointed != existing.commit_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"Tag '{tag_name}' already exists at {existing.commit_id[:12]}, "
                           f"not at {target}. Choose another tag name.",
                )
        taken = db.query(Release).filter(Release.tag_id == existing.id).first()
        if taken is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Tag '{tag_name}' already belongs to release {taken.version}",
            )
        the_tag = existing
    else:
        try:
            the_tag = create_tag(db, repository, actor, name=tag_name, target=target,
                                 message=f"Release {version}", commit=False)
        except HTTPException:
            db.rollback()
            raise

    release = Release(repository_id=repository.id, tag_id=the_tag.id, version=version,
                      title=(title or "").strip() or None, notes=(notes or "").strip() or None,
                      created_by_id=actor.id)
    db.add(release)
    db.commit()
    db.refresh(release)
    return release


def update_release(db: Session, release: Release, *, title: Optional[str] = None,
                   notes: Optional[str] = None) -> Dict[str, Any]:
    """Change a release's words. Its version and tag stay: they are what people cite."""
    changed = {}
    if title is not None and (title.strip() or None) != release.title:
        release.title = title.strip() or None
        changed["title"] = release.title
    if notes is not None and (notes.strip() or None) != release.notes:
        release.notes = notes.strip() or None
        changed["notes"] = True
    if changed:
        db.commit()
        db.refresh(release)
    return changed


def delete_release(db: Session, release: Release) -> Dict[str, Any]:
    """Withdraw a release. Its tag stays, as on GitHub: the commit keeps its name."""
    snapshot = {"version": release.version, "tag": release.tag.name if release.tag else None,
                "commit_id": release.tag.commit_id if release.tag else None}
    db.delete(release)
    db.commit()
    return snapshot


# --- notes --------------------------------------------------------------------------

def draft_notes(db: Session, repository: Repository, *, target: Optional[str] = None,
                previous_tag: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """Release notes drafted from what changed since the previous release.

    The previous release is the newest one whose commit is in the target's history,
    unless one is named. Merged pull requests are listed by title; commits that did not
    arrive through one are listed by their subject line.
    """
    head = repo_refs.resolve(db, repository, target)
    graph = repo_refs.CommitGraph(db, repository)
    in_target = graph.reachable(head["commit_id"]) or set()

    previous = None
    if previous_tag:
        previous = db.query(Tag).filter(Tag.repository_id == repository.id,
                                        Tag.name == previous_tag).first()
        if previous is None:
            raise HTTPException(status_code=404, detail=f"Tag '{previous_tag}' not found")
    else:
        for release in (
            db.query(Release).filter(Release.repository_id == repository.id)
            .order_by(Release.created_at.desc(), Release.id.desc()).all()
        ):
            if release.tag and release.tag.commit_id in in_target \
                    and release.tag.commit_id != head["commit_id"]:
                previous = release.tag
                break

    since = (graph.reachable(previous.commit_id) or set()) if previous else set()
    changed_ids = in_target - since
    commits = (
        db.query(Commit).filter(Commit.id.in_(list(changed_ids))).all()
        if changed_ids else []
    )
    commits.sort(key=lambda c: (c.created_at.timestamp() if c.created_at else 0, c.id), reverse=True)

    merged = (
        db.query(PullRequest)
        .filter(PullRequest.repository_id == repository.id,
                PullRequest.status == "merged",
                PullRequest.merge_commit_id.in_(list(changed_ids)))
        .order_by(PullRequest.id.desc())
        .all()
        if changed_ids else []
    )
    merge_commits = {pr.merge_commit_id for pr in merged}

    lines = ["## What's changed", ""]
    for pr in merged:
        author = pr.created_by.username if pr.created_by else None
        lines.append(f"- {pr.title} (#{pr.id})" + (f" by @{author}" if author else ""))
    listed = 0
    for commit in commits:
        if commit.id in merge_commits:
            continue
        if listed >= limit:
            break
        subject = repo_refs._subject(commit.message) or commit.id[:8]
        author = commit.author.username if commit.author else None
        lines.append(f"- {subject} ({commit.id[:7]})" + (f" by @{author}" if author else ""))
        listed += 1
    if len(lines) == 2:
        lines.append("- No changes since the previous release.")
    hidden = max(0, len(commits) - len(merge_commits) - listed)
    if hidden:
        lines.append(f"- …and {hidden} more commit(s)")
    lines.append("")
    if previous is not None:
        lines.append(f"**Full changelog**: {previous.name}...{head['name']} ({len(commits)} commits)")
    else:
        lines.append(f"**Full changelog**: {len(commits)} commits up to {head['name']}")

    return {
        "notes": "\n".join(lines),
        "target": head,
        "previous_tag": previous.name if previous is not None else None,
        "commit_count": len(commits),
        "pull_requests": [{"id": pr.id, "title": pr.title} for pr in merged],
    }
