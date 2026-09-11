"""Tests for repository, commit and code search."""
import os
import sys
import tempfile
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD  # noqa: E402
from database.models import Branch, Commit, CommitFile, FileObject  # noqa: E402
from app.services import search as sx  # noqa: E402


class SearchTestBase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.lead = UserCRUD.create_user(
            self.db, username="lead", email="l@e.com", role="team_lead")
        self.dev = UserCRUD.create_user(
            self.db, username="dev", email="d@e.com", role="developer")

        self.web = RepositoryCRUD.create_repository(self.db, "lead", "web", "The web client")
        self.api = RepositoryCRUD.create_repository(self.db, "lead", "api-server", "Backend for web")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def add_commit(self, repo, cid, message, author=None, files=None):
        c = Commit(id=cid, repository_id=repo.id, message=message,
                   author_id=(author or self.lead).id)
        self.db.add(c)
        self.db.flush()
        for path, content in (files or {}).items():
            blob = content if isinstance(content, bytes) else content.encode("utf-8")
            digest = f"h{abs(hash(path + str(len(blob)))):016x}"[:40]
            if not self.db.query(FileObject).filter(FileObject.hash == digest).first():
                self.db.add(FileObject(hash=digest, _content=blob, size=len(blob)))
            self.db.add(CommitFile(commit_id=cid, file_path=path, file_hash=digest))
        # Point both heads at it. With no branch named, the API resolves content
        # through Repository.head_commit_id rather than the default branch, so a
        # fixture that sets only the branch head finds nothing.
        branch = self.db.query(Branch).filter(
            Branch.repository_id == repo.id, Branch.name == "main").first()
        if branch:
            branch.head_commit_id = cid
        repo.head_commit_id = cid
        self.db.commit()
        return c


class RepositorySearchTests(SearchTestBase):
    def test_finds_by_name(self):
        r = sx.search_repositories(self.db, self.lead, query="web")
        self.assertIn("web", [x["name"] for x in r["repositories"]])

    def test_finds_by_description(self):
        r = sx.search_repositories(self.db, self.lead, query="Backend")
        self.assertEqual([x["name"] for x in r["repositories"]], ["api-server"])

    def test_exact_name_match_ranks_first(self):
        """A search for 'web' must not bury the repository called web."""
        r = sx.search_repositories(self.db, self.lead, query="web")
        self.assertEqual(r["repositories"][0]["name"], "web")

    def test_is_case_insensitive(self):
        self.assertTrue(sx.search_repositories(self.db, self.lead, query="WEB")["total"])

    def test_filter_by_owner_without_a_query(self):
        r = sx.search_repositories(self.db, self.lead, owner="lead")
        self.assertEqual(r["total"], 2)

    def test_unknown_owner_returns_nothing(self):
        self.assertEqual(sx.search_repositories(self.db, self.lead, owner="nobody")["total"], 0)

    def test_no_match_returns_empty_not_error(self):
        r = sx.search_repositories(self.db, self.lead, query="zzzznotathing")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["repositories"], [])

    def test_limit_is_respected_and_truncation_flagged(self):
        r = sx.search_repositories(self.db, self.lead, owner="lead", limit=1)
        self.assertEqual(len(r["repositories"]), 1)
        self.assertTrue(r["truncated"])


class CommitSearchTests(SearchTestBase):
    def setUp(self):
        super().setUp()
        self.add_commit(self.web, "c1", "Fix the login redirect")
        self.add_commit(self.web, "c2", "Add search endpoint", author=self.dev)
        self.add_commit(self.api, "c3", "Fix a crash on push")

    def test_finds_by_message(self):
        r = sx.search_commits(self.db, self.lead, query="login")
        self.assertEqual([c["id"] for c in r["commits"]], ["c1"])

    def test_matches_across_repositories(self):
        r = sx.search_commits(self.db, self.lead, query="Fix")
        self.assertEqual(len(r["commits"]), 2)

    def test_can_scope_to_one_repository(self):
        r = sx.search_commits(self.db, self.lead, query="Fix", repository_id=self.api.id)
        self.assertEqual([c["id"] for c in r["commits"]], ["c3"])

    def test_can_filter_by_author(self):
        r = sx.search_commits(self.db, self.lead, query="", author="dev")
        self.assertEqual([c["id"] for c in r["commits"]], ["c2"])

    def test_empty_query_without_author_is_refused(self):
        with self.assertRaises(sx.SearchError):
            sx.search_commits(self.db, self.lead, query="  ")

    def test_unknown_repository_is_404(self):
        with self.assertRaises(sx.SearchError) as ctx:
            sx.search_commits(self.db, self.lead, query="x", repository_id="nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_result_carries_repository_name(self):
        r = sx.search_commits(self.db, self.lead, query="login")
        self.assertEqual(r["commits"][0]["repository_name"], "web")


class CodeSearchTests(SearchTestBase):
    def setUp(self):
        super().setUp()
        self.add_commit(self.web, "c1", "seed", files={
            "src/app.py": "import os\nSECRET_KEY = 'abc'\nprint(SECRET_KEY)\n",
            "src/util.py": "def helper():\n    return 42\n",
            "README.md": "# Web\nThe SECRET_KEY lives in app.py\n",
            "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
        })

    def test_finds_a_literal_across_files(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "SECRET_KEY")
        paths = {x["path"] for x in r["results"]}
        self.assertEqual(paths, {"src/app.py", "README.md"})

    def test_reports_line_numbers(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "SECRET_KEY")
        app = next(x for x in r["results"] if x["path"] == "src/app.py")
        self.assertEqual([m["line"] for m in app["matches"]], [2, 3])

    def test_ranks_files_by_match_count(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "SECRET_KEY")
        self.assertEqual(r["results"][0]["path"], "src/app.py")

    def test_binary_files_are_skipped_not_searched(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "PNG")
        self.assertEqual(r["results"], [])
        self.assertGreaterEqual(r["files_skipped"], 1)

    def test_is_case_insensitive_by_default(self):
        self.assertTrue(sx.search_code(self.db, self.lead, self.web.id, "secret_key")["total_matches"])

    def test_case_sensitive_mode_narrows(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "secret_key", case_sensitive=True)
        self.assertEqual(r["total_matches"], 0)

    def test_a_plain_query_is_a_literal_not_a_regex(self):
        """Typing a.b must mean those three characters."""
        r = sx.search_code(self.db, self.lead, self.web.id, "app.py")
        self.assertTrue(r["total_matches"])
        r2 = sx.search_code(self.db, self.lead, self.web.id, "appxpy")
        self.assertEqual(r2["total_matches"], 0)

    def test_regex_mode_works(self):
        r = sx.search_code(self.db, self.lead, self.web.id, r"SECRET_\w+", regex=True)
        self.assertTrue(r["total_matches"])

    def test_invalid_regex_is_a_caller_error(self):
        with self.assertRaises(sx.SearchError) as ctx:
            sx.search_code(self.db, self.lead, self.web.id, "([oops", regex=True)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_path_filter_narrows_results(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "SECRET_KEY", path_filter="README")
        self.assertEqual([x["path"] for x in r["results"]], ["README.md"])

    def test_empty_query_is_refused(self):
        with self.assertRaises(sx.SearchError):
            sx.search_code(self.db, self.lead, self.web.id, "   ")

    def test_unknown_repository_is_404(self):
        with self.assertRaises(sx.SearchError) as ctx:
            sx.search_code(self.db, self.lead, "nope", "x")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_repository_with_no_commits_returns_empty(self):
        r = sx.search_code(self.db, self.lead, self.api.id, "anything")
        self.assertEqual(r["results"], [])
        self.assertIsNone(r["commit_id"])

    def test_limit_caps_matches_and_flags_truncation(self):
        r = sx.search_code(self.db, self.lead, self.web.id, "SECRET_KEY", limit=1)
        self.assertEqual(r["total_matches"], 1)
        self.assertTrue(r["truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
