"""Tests for user profiles.

Two things here are security-relevant rather than cosmetic. An avatar is identified by
its own leading bytes, not by what the uploader called it, so an HTML file cannot be
stored as an image. And the repository list is filtered to what the viewer may already
see, so a profile page cannot be used to enumerate private repositories by owner.
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

from app.services import blob_store, profiles  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Commit, CommitFile, PullRequest, Repository, User, UserPermission,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 64


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_prof_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_prof_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.alice = User(username="alice", role="developer", email="alice@example.com")
        self.bob = User(username="bob", role="developer", email="bob@example.com")
        self.admin = User(username="root", role="admin", email="root@example.com")
        self.db.add_all([self.alice, self.bob, self.admin])
        self.db.commit()

        self.repo = Repository(id="r1", name="alices-repo", owner_id=self.alice.id,
                               description="hers")
        self.private = Repository(id="r2", name="alices-secret", owner_id=self.alice.id)
        self.db.add_all([self.repo, self.private])
        self.db.commit()

        self.mk("c1", "r1", self.alice)
        self.mk("c2", "r1", self.alice)

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, repo_id, author):
        self.db.add(Commit(id=cid, repository_id=repo_id, author_id=author.id,
                           message=f"{cid} did a thing"))
        self.db.flush()
        obj = FileObjectCRUD.store_file_object(self.db, f"{cid}\n".encode())
        self.db.add(CommitFile(commit_id=cid, file_path="a.txt",
                               file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

    # --- avatars ----------------------------------------------------------------

    def test_an_image_is_identified_by_its_own_bytes(self):
        self.assertEqual(profiles.detect_image_type(PNG), "image/png")
        self.assertEqual(profiles.detect_image_type(JPEG), "image/jpeg")
        self.assertEqual(profiles.detect_image_type(WEBP), "image/webp")

    def test_a_file_that_is_not_an_image_is_refused(self):
        """The declared content type comes from the uploader; the bytes do not."""
        with self.assertRaises(profiles.ProfileError):
            profiles.store_avatar(self.db, self.alice, b"<html>not an image</html>")
        self.assertIsNone(self.alice.avatar_blob_hash)

    def test_an_oversized_avatar_is_refused(self):
        huge = PNG + b"\x00" * (profiles.MAX_AVATAR_BYTES + 1)
        with self.assertRaises(profiles.ProfileError) as caught:
            profiles.store_avatar(self.db, self.alice, huge)
        self.assertEqual(caught.exception.status_code, 413)

    def test_an_empty_upload_is_refused(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.store_avatar(self.db, self.alice, b"")

    def test_a_stored_avatar_reads_back_unchanged(self):
        profiles.store_avatar(self.db, self.alice, PNG)
        self.assertEqual(profiles.read_avatar(self.alice), PNG)
        self.assertEqual(self.alice.avatar_content_type, "image/png")

    def test_clearing_an_avatar_leaves_the_blob_for_anyone_sharing_it(self):
        """Blobs are content-addressed, so deleting one could blank another user."""
        profiles.store_avatar(self.db, self.alice, PNG)
        profiles.store_avatar(self.db, self.bob, PNG)
        blob = self.alice.avatar_blob_hash

        profiles.clear_avatar(self.db, self.alice)
        self.assertIsNone(self.alice.avatar_blob_hash)
        self.assertEqual(profiles.read_avatar(self.bob), PNG)
        self.assertTrue(blob_store.store.exists(blob))

    def test_no_avatar_reads_as_none(self):
        self.assertIsNone(profiles.read_avatar(self.bob))

    # --- editing ----------------------------------------------------------------

    def test_a_person_can_change_their_own_details(self):
        profiles.update_profile(self.db, self.alice, {
            "full_name": "Alice Example", "bio": "I write things",
            "location": "Lahore", "website": "https://example.com",
        })
        self.assertEqual(self.alice.full_name, "Alice Example")
        self.assertEqual(self.alice.bio, "I write things")
        self.assertEqual(self.alice.location, "Lahore")

    def test_a_website_must_be_a_url(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.update_profile(self.db, self.alice, {"website": "example.com"})

    def test_an_over_long_bio_is_refused(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.update_profile(self.db, self.alice, {"bio": "x" * 1001})

    def test_an_email_without_an_at_sign_is_refused(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.update_profile(self.db, self.alice, {"email": "nope"})

    def test_updating_cannot_change_role_or_active_state(self):
        """Self-service must not become a way to grant yourself permissions."""
        profiles.update_profile(self.db, self.alice, {
            "full_name": "Alice", "role": "admin", "is_active": False,
        })
        self.assertEqual(self.alice.role, "developer")
        self.assertTrue(self.alice.is_active)

    def test_clearing_a_field_sets_it_to_nothing_rather_than_empty_text(self):
        profiles.update_profile(self.db, self.alice, {"bio": "something"})
        profiles.update_profile(self.db, self.alice, {"bio": "   "})
        self.assertIsNone(self.alice.bio)

    # --- what a viewer sees -----------------------------------------------------

    def test_your_own_profile_shows_your_email(self):
        self.assertEqual(
            profiles.identity(self.alice, viewer=self.alice)["email"],
            "alice@example.com")

    def test_someone_elses_profile_hides_their_email(self):
        self.assertIsNone(profiles.identity(self.alice, viewer=self.bob)["email"])

    def test_an_admin_can_see_an_email(self):
        self.assertEqual(
            profiles.identity(self.alice, viewer=self.admin)["email"],
            "alice@example.com")

    def test_a_profile_knows_whether_it_is_your_own(self):
        self.assertTrue(profiles.identity(self.alice, viewer=self.alice)["is_self"])
        self.assertFalse(profiles.identity(self.alice, viewer=self.bob)["is_self"])

    # --- repositories and stats -------------------------------------------------

    def test_your_own_profile_lists_every_repository_you_own(self):
        built = profiles.build(self.db, self.alice, viewer=self.alice)
        self.assertEqual(
            sorted(r["name"] for r in built["repositories"]),
            ["alices-repo", "alices-secret"])

    def test_a_stranger_sees_none_of_your_private_repositories(self):
        """A profile must not become a way to enumerate repositories by owner."""
        built = profiles.build(self.db, self.alice, viewer=self.bob)
        self.assertEqual(built["repositories"], [])

    def test_a_collaborator_sees_the_repository_they_collaborate_on(self):
        self.db.add(UserPermission(repository_id="r1", user_id=self.bob.id,
                                   permission_level="write"))
        self.db.commit()
        built = profiles.build(self.db, self.alice, viewer=self.bob)
        self.assertEqual([r["name"] for r in built["repositories"]], ["alices-repo"])

    def test_an_admin_sees_everything(self):
        built = profiles.build(self.db, self.alice, viewer=self.admin)
        self.assertEqual(len(built["repositories"]), 2)

    def test_the_commit_total_is_the_real_number(self):
        """The list is filtered by visibility; the count should not lie about it."""
        built = profiles.build(self.db, self.alice, viewer=self.bob)
        self.assertEqual(built["stats"]["commits"], 2)
        self.assertEqual(built["stats"]["commits_visible"], 0)

    def test_recent_commits_are_listed_for_a_viewer_who_may_see_them(self):
        built = profiles.build(self.db, self.alice, viewer=self.alice)
        self.assertEqual(len(built["recent_commits"]), 2)
        self.assertEqual(built["recent_commits"][0]["repository_name"], "alices-repo")

    def test_pull_requests_are_counted(self):
        self.db.add(PullRequest(repository_id="r1", title="t", source_branch="a",
                                target_branch="b", status="open",
                                created_by_id=self.alice.id))
        self.db.commit()
        self.assertEqual(
            profiles.build(self.db, self.alice, viewer=self.alice)["stats"]["pull_requests"],
            1)

    def test_a_user_with_nothing_still_builds_a_profile(self):
        built = profiles.build(self.db, self.bob, viewer=self.bob)
        self.assertEqual(built["stats"]["repositories"], 0)
        self.assertEqual(built["stats"]["commits"], 0)
        self.assertEqual(built["recent_commits"], [])


if __name__ == "__main__":
    unittest.main()
