"""Tests for branch policy, publish, and copy-files APIs."""
import unittest

from branch_policies import (
    actor_meets_scope,
    default_branch_policy,
    get_branch_policy,
    is_ancestor,
    is_protected_branch,
)
from database.database import SessionLocal, create_tables
from database.crud import BranchCRUD, RepositoryCRUD, UserCRUD
from database.models import Repository, User


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
        create_tables()
        from server import ensure_repositories_branch_policy_column

        ensure_repositories_branch_policy_column()
        cls.db = SessionLocal()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.owner = UserCRUD.create_user(
            self.db, "branch_feat_owner", "owner@test.com"
        )
        self.repo = RepositoryCRUD.create_repository(
            self.db, self.owner.username, "branch_feat_repo", "test"
        )

    def test_get_branch_policy_empty_repo(self):
        policy = get_branch_policy(self.repo)
        self.assertEqual(policy["create_branch_min_scope"], "write")
        self.assertTrue(actor_meets_scope(self.db, self.owner, self.repo, "write"))


if __name__ == "__main__":
    unittest.main()
