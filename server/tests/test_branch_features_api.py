"""Tests for branch policy helpers.

This suite has never passed: it imported `ensure_repositories_branch_policy_column` from
`server`, and that function did not exist until the branch-policy column was added. It
also ran against the *live* database via `SessionLocal` and `create_tables()`, so simply
fixing the import would have made it write users and repositories into production. It now
builds its own temporary database.
"""
import os
import sys
import tempfile
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from branch_policies import (  # noqa: E402
    actor_meets_scope,
    default_branch_policy,
    get_branch_policy,
    is_protected_branch,
)
from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD  # noqa: E402


class BranchPoliciesUnitTests(unittest.TestCase):
    def test_default_policy_keys(self):
        p = default_branch_policy()
        self.assertIn("create_branch_min_scope", p)
        self.assertIn("protected_branches", p)

    def test_protected_default_branch(self):
        p = default_branch_policy()
        self.assertTrue(is_protected_branch(p, "main", "main"))
        self.assertFalse(is_protected_branch(p, "feature", "main"))


class BranchFeaturesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(prefix="foxnest_bf_", suffix=".db")
        os.close(fd)
        cls.engine = create_engine("sqlite:///" + cls.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=cls.engine)
        cls.Session = sessionmaker(bind=cls.engine)
        cls.db = cls.Session()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.engine.dispose()
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def setUp(self):
        self.owner = UserCRUD.create_user(self.db, "branch_feat_owner", "owner@test.com")
        self.repo = RepositoryCRUD.create_repository(
            self.db, self.owner.username, "branch_feat_repo", "test"
        )

    def tearDown(self):
        # Each test builds its own owner/repository, so clear them between runs.
        self.db.rollback()
        for row in self.db.query(type(self.repo)).all():
            self.db.delete(row)
        for row in self.db.query(type(self.owner)).all():
            self.db.delete(row)
        self.db.commit()

    def test_the_branch_policy_column_exists(self):
        # The column `branch_policies.get_branch_policy` reads. It was missing entirely
        # until branch protection was wired up, so the policy could never be persisted.
        columns = [c["name"] for c in inspect(self.engine).get_columns("repositories")]
        self.assertIn("branch_policy_json", columns)

    def test_get_branch_policy_empty_repo(self):
        policy = get_branch_policy(self.repo)
        self.assertEqual(policy["create_branch_min_scope"], "write")
        self.assertTrue(actor_meets_scope(self.db, self.owner, self.repo, "write"))

    def test_a_stored_policy_overrides_the_defaults(self):
        self.repo.branch_policy_json = '{"merge_min_scope": "team_lead"}'
        self.db.commit()

        policy = get_branch_policy(self.repo)
        self.assertEqual(policy["merge_min_scope"], "team_lead")
        # unspecified keys still fall back
        self.assertEqual(policy["push_min_scope"], "write")

    def test_a_corrupt_policy_falls_back_to_defaults(self):
        self.repo.branch_policy_json = "{not json"
        self.db.commit()
        self.assertEqual(get_branch_policy(self.repo), default_branch_policy())


if __name__ == "__main__":
    unittest.main()
