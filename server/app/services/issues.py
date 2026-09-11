"""Issue serialisation, per-actor permissions, and commit/PR cross-linking."""

import json

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.crud import IssueCRUD, IssueEventCRUD, IssueLinkCRUD, parse_issue_references
from database.models import Commit, Issue, IssueComment, PullRequest, Repository, User

from app.core.permissions import can_manage_repository
from app.services.branches import _get_default_branch


def _issue_comment_count(db: Session, issue_id: int) -> int:
    return int(db.query(func.count(IssueComment.id)).filter(IssueComment.issue_id == issue_id).scalar() or 0)


def _issue_to_list_dict(db: Session, issue: Issue) -> Dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "assigned_to": issue.assigned_to.username if issue.assigned_to else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "comment_count": _issue_comment_count(db, issue.id),
    }


def _issue_to_detail_dict(db: Session, repo_id: str, issue: Issue) -> Dict[str, Any]:
    labels = []
    for link in issue.label_links or []:
        if link.label:
            labels.append({"name": link.label.name, "color": link.label.color})

    comments_out = []
    for c in sorted(issue.comments or [], key=lambda x: x.created_at or datetime.min):
        comments_out.append({
            "id": c.id,
            "body": c.body,
            "author": c.author.username if c.author else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    events_out = []
    for ev in IssueEventCRUD.list_events(db, issue.id, limit=200):
        payload = {}
        if ev.payload_json:
            try:
                payload = json.loads(ev.payload_json)
            except Exception:
                payload = {}
        events_out.append({
            "id": ev.id,
            "type": ev.event_type,
            "actor": ev.actor.username if ev.actor else None,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
            "payload": payload,
        })

    branches = [{"name": b.branch_name} for b in IssueLinkCRUD.list_linked_branches(db, repo_id, issue.id)]
    prs = []
    for link in IssueLinkCRUD.list_linked_pull_requests(db, issue.id):
        pr = db.query(PullRequest).filter(PullRequest.id == link.pull_request_id).first()
        if pr:
            prs.append({"id": pr.id, "title": pr.title, "status": pr.status, "link_type": link.link_type})
    commits_out = []
    for link in IssueLinkCRUD.list_linked_commits(db, issue.id):
        c = db.query(Commit).filter(Commit.id == link.commit_id).first()
        if c:
            commits_out.append({"id": c.id, "message": c.message, "link_type": link.link_type})

    return {
        "number": issue.number,
        "title": issue.title,
        "description": issue.description,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "created_by": issue.created_by.username if issue.created_by else None,
        "assigned_to": issue.assigned_to.username if issue.assigned_to else None,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "labels": labels,
        "comments": comments_out,
        "events": events_out,
        "links": {
            "branches": branches,
            "pull_requests": prs,
            "commits": commits_out,
        },
    }


def _issue_permissions_for_actor(db: Session, actor: User, repository: Repository, issue: Issue) -> Dict[str, Any]:
    """Return a lightweight permission matrix for issue actions.

    Good-practice policy:
    - Closing is allowed for repository managers and the issue creator.
    - Reopening remains restricted to repository managers (manage-level).
    - Assignees/creators can move work between open/in_progress/resolved.
    """
    can_manage = can_manage_repository(db, actor, repository)
    is_assignee = bool(issue.assigned_to_id and actor and actor.id == issue.assigned_to_id)
    is_creator = bool(issue.created_by_id and actor and actor.id == issue.created_by_id)
    can_update_work_status = can_manage or is_assignee or is_creator
    return {
        "can_close": bool(can_manage or is_creator),
        "can_reopen": bool(can_manage),
        "can_update_work_status": bool(can_update_work_status),
    }


def _sync_issues_for_new_commit(db: Session, repo_id: str, commit: Commit, branch_name: str, actor_id: int) -> None:
    default_branch = _get_default_branch(db, repo_id)
    is_default = bool(default_branch and default_branch.name == branch_name)
    text = commit.message or ""
    for ref in parse_issue_references(text):
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_commit(db, issue.id, commit.id, ref["link_type"])
        IssueLinkCRUD.link_branch(db, repo_id, issue.id, branch_name)
        if ref["close"] and is_default:
            try:
                IssueCRUD.transition_status(db, issue, "closed", actor_id=actor_id)
            except ValueError:
                pass


def _sync_issues_for_pr_created(db: Session, repo_id: str, pr: PullRequest) -> None:
    text = f"{pr.title or ''}\n{pr.description or ''}"
    for ref in parse_issue_references(text):
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_pull_request(db, issue.id, pr.id, ref["link_type"])


def _sync_issues_for_pr_merged(db: Session, repo_id: str, pr: PullRequest, merge_commit: Commit, actor_id: int) -> None:
    text = f"{pr.title or ''}\n{pr.description or ''}\n{merge_commit.message or ''}"
    for ref in parse_issue_references(text):
        if not ref["close"]:
            continue
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_commit(db, issue.id, merge_commit.id, "closes")
        IssueLinkCRUD.link_pull_request(db, issue.id, pr.id, "closes")
        try:
            IssueCRUD.transition_status(db, issue, "closed", actor_id=actor_id)
        except ValueError:
            pass
