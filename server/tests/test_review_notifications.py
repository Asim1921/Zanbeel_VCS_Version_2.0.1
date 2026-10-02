"""Tests for review-workflow notifications.

The review features shipped without any of this, so the gate worked and nobody was
told. These pin down who gets told what — and, as much, who does not: telling someone
about their own action is noise, and noise is how a notification list stops being read.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services import blob_store, review_notifications as notify  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.models import (  # noqa: E402
    Notification, PullRequest, PullRequestComment, Repository, User,
)


class ReviewNotificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_notif_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_notif_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="developer")
        self.reviewer = User(username="reviewer", role="team_lead")
        self.owner = User(username="owner", role="team_lead")
        self.third = User(username="third", role="developer")
        self.db.add_all([self.author, self.reviewer, self.owner, self.third])
        self.db.commit()

        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        self.pr = PullRequest(repository_id="r1", title="add a thing",
                              source_branch="feature", target_branch="main",
                              status="open", created_by_id=self.author.id)
        self.db.add(self.pr)
        self.db.commit()
        self.db.refresh(self.pr)

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def inbox(self, user):
        return (
            self.db.query(Notification)
            .filter(Notification.user_id == user.id)
            .order_by(Notification.id)
            .all()
        )

    def comment(self, author, body="looks off", in_reply_to=None, line=42):
        row = PullRequestComment(
            pull_request_id=self.pr.id, author_id=author.id,
            file_path="app.py", line=line, side="new", body=body,
            in_reply_to_id=in_reply_to,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    # --- reviews ----------------------------------------------------------------

    def test_an_approval_tells_the_author(self):
        notify.review_submitted(self.db, self.repo, self.pr, self.reviewer, "approved")
        inbox = self.inbox(self.author)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].notification_type, notify.TYPE_REVIEW_SUBMITTED)
        self.assertIn("approved", inbox[0].title)
        self.assertIn("reviewer", inbox[0].title)

    def test_requested_changes_reads_as_such(self):
        notify.review_submitted(self.db, self.repo, self.pr, self.reviewer,
                                "changes_requested")
        self.assertIn("requested changes", self.inbox(self.author)[0].title)

    def test_reviewing_your_own_pull_request_tells_nobody(self):
        """Telling someone what they just did is noise."""
        notify.review_submitted(self.db, self.repo, self.pr, self.author, "commented")
        self.assertEqual(self.inbox(self.author), [])

    def test_the_same_review_twice_does_not_stack_up(self):
        for _ in range(3):
            notify.review_submitted(self.db, self.repo, self.pr, self.reviewer, "approved")
        self.assertEqual(len(self.inbox(self.author)), 1)

    def test_a_different_verdict_is_a_separate_notification(self):
        notify.review_submitted(self.db, self.repo, self.pr, self.reviewer, "approved")
        notify.review_submitted(self.db, self.repo, self.pr, self.reviewer,
                                "changes_requested")
        self.assertEqual(len(self.inbox(self.author)), 2)

    def test_the_payload_carries_enough_to_open_the_pull_request(self):
        notify.review_submitted(self.db, self.repo, self.pr, self.reviewer, "approved")
        import json
        payload = json.loads(self.inbox(self.author)[0].payload_json)
        self.assertEqual(payload["pull_request_id"], self.pr.id)
        self.assertEqual(payload["repository_id"], "r1")
        self.assertEqual(payload["reviewer"], "reviewer")

    # --- merges -----------------------------------------------------------------

    def test_a_merge_tells_the_author(self):
        notify.pull_request_merged(self.db, self.repo, self.pr, self.reviewer, "abc123")
        inbox = self.inbox(self.author)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].notification_type, notify.TYPE_PR_MERGED)

    def test_merging_your_own_pull_request_tells_nobody(self):
        notify.pull_request_merged(self.db, self.repo, self.pr, self.author, "abc123")
        self.assertEqual(self.inbox(self.author), [])

    def test_a_merge_is_announced_once(self):
        notify.pull_request_merged(self.db, self.repo, self.pr, self.reviewer, "abc")
        notify.pull_request_merged(self.db, self.repo, self.pr, self.reviewer, "abc")
        self.assertEqual(len(self.inbox(self.author)), 1)

    # --- comments ---------------------------------------------------------------

    def test_a_comment_tells_the_author(self):
        notify.comment_added(self.db, self.repo, self.pr, self.reviewer,
                             self.comment(self.reviewer))
        inbox = self.inbox(self.author)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].notification_type, notify.TYPE_PR_COMMENT)
        self.assertIn("app.py:42", inbox[0].body)

    def test_your_own_comment_does_not_notify_you(self):
        notify.comment_added(self.db, self.repo, self.pr, self.author,
                             self.comment(self.author))
        self.assertEqual(self.inbox(self.author), [])

    def test_a_reply_reaches_the_person_being_replied_to(self):
        """Only notifying the author would leave a reviewer waiting on their own thread."""
        root = self.comment(self.reviewer, "why this?")
        reply = self.comment(self.third, "because x", in_reply_to=root.id)
        notify.comment_added(self.db, self.repo, self.pr, self.third, reply)

        self.assertEqual(len(self.inbox(self.reviewer)), 1)
        self.assertEqual(len(self.inbox(self.author)), 1)
        self.assertEqual(self.inbox(self.third), [])

    def test_a_reply_by_the_thread_starter_does_not_notify_themselves(self):
        root = self.comment(self.reviewer, "why this?")
        reply = self.comment(self.reviewer, "never mind", in_reply_to=root.id)
        notify.comment_added(self.db, self.repo, self.pr, self.reviewer, reply)
        self.assertEqual(self.inbox(self.reviewer), [])

    # --- code owners ------------------------------------------------------------

    def test_code_owners_are_told_their_review_is_required(self):
        """CODEOWNERS blocks the merge on them, so they have to know."""
        notify.review_requested_from_owners(self.db, self.repo, self.pr, ["owner"])
        inbox = self.inbox(self.owner)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].notification_type, notify.TYPE_REVIEW_REQUESTED)

    def test_an_owner_who_wrote_the_pull_request_is_not_asked_to_review_it(self):
        notify.review_requested_from_owners(self.db, self.repo, self.pr, ["author"])
        self.assertEqual(self.inbox(self.author), [])

    def test_an_owner_who_does_not_exist_is_skipped_quietly(self):
        """CODEOWNERS can name someone with no account; that must not break a push."""
        notify.review_requested_from_owners(self.db, self.repo, self.pr, ["ghost"])
        self.assertEqual(self.db.query(Notification).count(), 0)

    def test_owners_are_asked_once_per_pull_request(self):
        notify.review_requested_from_owners(self.db, self.repo, self.pr, ["owner"])
        notify.review_requested_from_owners(self.db, self.repo, self.pr, ["owner"])
        self.assertEqual(len(self.inbox(self.owner)), 1)

    # --- resilience --------------------------------------------------------------

    def test_a_notification_failure_does_not_raise(self):
        """Losing a merge because a notification row failed would be far worse."""
        broken = PullRequest(repository_id="r1", title="t", source_branch="a",
                             target_branch="b", status="open", created_by_id=999999)
        self.db.add(broken)
        self.db.commit()
        # 999999 is not a real user; the foreign key will reject the row.
        notify.pull_request_merged(self.db, self.repo, broken, self.reviewer, "abc")


if __name__ == "__main__":
    unittest.main()
