"""Tests for CODEOWNERS parsing and the ownership half of the merge gate."""
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

from app.services import blob_store, codeowners, pr_reviews  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, PullRequest, Repository, User,
)


class CodeownersParsingTests(unittest.TestCase):
    def test_basic_rules(self):
        rules = codeowners.parse("*  @lead\n/db/  @alice @bob\n")
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].owners, ["lead"])
        self.assertEqual(rules[1].owners, ["alice", "bob"])

    def test_comments_and_blanks_are_skipped(self):
        rules = codeowners.parse("# a comment\n\n   \n*.py @dev  # trailing\n")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].owners, ["dev"])

    def test_a_pattern_without_owners_is_skipped(self):
        self.assertEqual(codeowners.parse("*.sql\n"), [])

    def test_the_at_sign_is_optional(self):
        rules = codeowners.parse("* alice\n")
        self.assertEqual(rules[0].owners, ["alice"])

    def test_last_matching_rule_wins(self):
        rules = codeowners.parse("*  @lead\n*.sql  @dba\n")
        self.assertEqual(codeowners.owners_for(rules, "schema.sql"), ["dba"])
        self.assertEqual(codeowners.owners_for(rules, "main.py"), ["lead"])

    def test_directory_rules_cover_their_contents(self):
        rules = codeowners.parse("/server/database/  @alice\n")
        self.assertEqual(codeowners.owners_for(rules, "server/database/models.py"), ["alice"])
        self.assertEqual(codeowners.owners_for(rules, "server/api/routes.py"), [])

    def test_unmatched_path_has_no_owner(self):
        rules = codeowners.parse("/docs/  @writer\n")
        self.assertEqual(codeowners.owners_for(rules, "src/main.py"), [])

    def test_empty_file(self):
        self.assertEqual(codeowners.parse(""), [])
        self.assertEqual(codeowners.parse(None), [])


class CodeownersGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_co_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_co_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.alice = User(username="alice", role="team_lead")
        self.bob = User(username="bob", role="team_lead")
        self.db.add_all([self.author, self.alice, self.bob])
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, files, parent=None):
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.author.id,
                           message=cid, parent_commit_id=parent))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

    def setup_pr(self, owners_file, base_files, feature_files):
        base = dict(base_files)
        if owners_file is not None:
            base["CODEOWNERS"] = owners_file
        feat = dict(base)
        feat.update(feature_files)

        self.mk("base", base)
        self.mk("feat", feat, "base")
        self.db.add(Branch(repository_id="r1", name="main", head_commit_id="base",
                           is_default=True))
        self.db.add(Branch(repository_id="r1", name="feature", head_commit_id="feat"))
        pr = PullRequest(repository_id="r1", title="t", source_branch="feature",
                         target_branch="main", status="open", created_by_id=self.author.id)
        self.db.add(pr)
        self.db.commit()
        self.db.refresh(pr)
        return pr

    # ---------------- requirement computation ----------------

    def test_no_codeowners_file_means_no_requirement(self):
        pr = self.setup_pr(None, {"a.py": "1"}, {"a.py": "2"})
        result = codeowners.required_owners(self.db, self.repo, pr)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["required"], [])

    def test_owners_are_derived_from_changed_paths(self):
        pr = self.setup_pr("db/  @alice\n", {"db/schema.sql": "a", "web/app.js": "x"},
                           {"db/schema.sql": "b"})
        result = codeowners.required_owners(self.db, self.repo, pr)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["required"], ["alice"])
        self.assertIn("db/schema.sql", result["by_path"])

    def test_untouched_paths_do_not_pull_in_their_owners(self):
        pr = self.setup_pr("db/  @alice\nweb/  @bob\n",
                           {"db/schema.sql": "a", "web/app.js": "x"},
                           {"web/app.js": "y"})
        self.assertEqual(codeowners.required_owners(self.db, self.repo, pr)["required"], ["bob"])

    def test_the_author_is_never_a_required_owner(self):
        # They cannot approve their own PR, so requiring them would deadlock it.
        pr = self.setup_pr("*  @author @alice\n", {"a.py": "1"}, {"a.py": "2"})
        self.assertEqual(codeowners.required_owners(self.db, self.repo, pr)["required"], ["alice"])

    def test_owners_who_are_not_users_are_reported_separately(self):
        pr = self.setup_pr("*  @ghost\n", {"a.py": "1"}, {"a.py": "2"})
        result = codeowners.required_owners(self.db, self.repo, pr)
        self.assertEqual(result["required"], [])
        self.assertEqual(result["unknown_owners"], ["ghost"])

    def test_changed_paths_include_additions(self):
        pr = self.setup_pr("*  @alice\n", {"a.py": "1"}, {"new.py": "brand new"})
        self.assertIn("new.py", codeowners.required_owners(self.db, self.repo, pr)["changed_paths"])

    # ---------------- the gate ----------------

    def test_an_unapproved_owner_blocks_the_merge(self):
        pr = self.setup_pr("db/  @alice\n", {"db/s.sql": "a"}, {"db/s.sql": "b"})
        summary = pr_reviews.summarize(self.db, self.repo, pr)
        self.assertFalse(summary["can_merge"])
        self.assertTrue(any("code owner" in b for b in summary["blockers"]))
        self.assertEqual(summary["code_owners"]["missing"], ["alice"])

    def test_the_owners_approval_unblocks_it(self):
        pr = self.setup_pr("db/  @alice\n", {"db/s.sql": "a"}, {"db/s.sql": "b"})
        pr_reviews.submit(self.db, self.repo, pr, self.alice, "approved")
        self.assertTrue(pr_reviews.summarize(self.db, self.repo, pr)["can_merge"])

    def test_approval_from_the_wrong_person_does_not_satisfy_ownership(self):
        # This is the point of code owners: a count alone lets anyone wave work through.
        pr = self.setup_pr("db/  @alice\n", {"db/s.sql": "a"}, {"db/s.sql": "b"})
        pr_reviews.submit(self.db, self.repo, pr, self.bob, "approved")
        summary = pr_reviews.summarize(self.db, self.repo, pr)
        self.assertFalse(summary["can_merge"])
        self.assertEqual(summary["code_owners"]["missing"], ["alice"])

    def test_a_stale_owner_approval_stops_counting(self):
        pr = self.setup_pr("db/  @alice\n", {"db/s.sql": "a"}, {"db/s.sql": "b"})
        pr_reviews.submit(self.db, self.repo, pr, self.alice, "approved")
        self.assertTrue(pr_reviews.summarize(self.db, self.repo, pr)["can_merge"])

        self.mk("feat2", {"db/s.sql": "c", "CODEOWNERS": "db/  @alice\n"}, "feat")
        from database.crud import BranchCRUD
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        summary = pr_reviews.summarize(self.db, self.repo, pr)
        self.assertFalse(summary["can_merge"])
        self.assertEqual(summary["code_owners"]["missing"], ["alice"])

    def test_multiple_owners_all_have_to_sign_off(self):
        pr = self.setup_pr("db/  @alice @bob\n", {"db/s.sql": "a"}, {"db/s.sql": "b"})
        pr_reviews.submit(self.db, self.repo, pr, self.alice, "approved")
        self.assertFalse(pr_reviews.summarize(self.db, self.repo, pr)["can_merge"])
        pr_reviews.submit(self.db, self.repo, pr, self.bob, "approved")
        self.assertTrue(pr_reviews.summarize(self.db, self.repo, pr)["can_merge"])


if __name__ == "__main__":
    unittest.main()
