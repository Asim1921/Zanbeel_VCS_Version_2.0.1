import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure imports work with the server module's absolute imports.
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
spec = importlib.util.spec_from_file_location('foxnest_server_module', SERVER_FILE)
server_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server_module)

from database.database import Base
from database.crud import UserCRUD, RepositoryCRUD, CommitCRUD, BranchCRUD


class VersioningApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, db_path = tempfile.mkstemp(prefix='foxnest_test_', suffix='.db')
        os.close(fd)
        cls._db_path = db_path

        cls.engine = create_engine(f"sqlite:///{db_path}")
        cls.TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        cls.original_startup = list(server_module.app.router.on_startup)
        server_module.app.router.on_startup = []

        def override_get_db():
            db = cls.TestSessionLocal()
            try:
                yield db
            finally:
                db.close()

        server_module.app.dependency_overrides[server_module.get_db] = override_get_db

        cls.client = TestClient(server_module.app)

        cls._seed_data()

    @classmethod
    def tearDownClass(cls):
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls.original_startup
        cls.client.close()
        cls.engine.dispose()
        if os.path.exists(cls._db_path):
            os.remove(cls._db_path)

    @classmethod
    def _seed_data(cls):
        db = cls.TestSessionLocal()
        try:
            password_hash = server_module.hash_password('Password123!')
            cls.user = UserCRUD.create_user(
                db,
                username='version_admin',
                role='team_lead',
                password_hash=password_hash,
                email='admin@example.com'
            )

            repo = RepositoryCRUD.create_repository(db, 'version_admin', 'versioning_repo', 'Versioning test repo')
            cls.repo_id = repo.id

            main_branch = BranchCRUD.get_branch(db, repo.id, 'main')

            c1 = CommitCRUD.create_commit(db, {
                'id': 'a' * 40,
                'repository_id': repo.id,
                'author': 'version_admin',
                'message': 'initial version',
                'files': {
                    'src/sample.txt': base64.b64encode(b'line1\nline2\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'main', c1.id)

            c2 = CommitCRUD.create_commit(db, {
                'id': 'b' * 40,
                'repository_id': repo.id,
                'author': 'version_admin',
                'parent': c1.id,
                'parents': [c1.id],
                'message': 'update line2',
                'files': {
                    'src/sample.txt': base64.b64encode(b'line1\nline2-updated\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'main', c2.id)

            BranchCRUD.create_branch(db, repo.id, 'feature_old', head_commit_id=c1.id)

            cls.commit_old = c1.id
            cls.commit_new = c2.id
            cls.auth_token = server_module.create_access_token('version_admin', 'team_lead')
        finally:
            db.close()

    def test_branch_aware_file_state(self):
        main_response = self.client.get(f'/api/repository/{self.repo_id}/files', params={'branch': 'main'})
        self.assertEqual(main_response.status_code, 200)
        main_payload = main_response.json()
        self.assertTrue(main_payload['success'])
        self.assertEqual(main_payload['files']['src/sample.txt']['content'], 'line1\nline2-updated\n')

        feature_response = self.client.get(f'/api/repository/{self.repo_id}/files', params={'branch': 'feature_old'})
        self.assertEqual(feature_response.status_code, 200)
        feature_payload = feature_response.json()
        self.assertTrue(feature_payload['success'])
        self.assertEqual(feature_payload['files']['src/sample.txt']['content'], 'line1\nline2\n')

    def test_file_history_and_compare(self):
        history_response = self.client.get(
            f'/api/repository/{self.repo_id}/file-history',
            params={'path': 'src/sample.txt', 'branch': 'main'}
        )
        self.assertEqual(history_response.status_code, 200)
        history_payload = history_response.json()
        self.assertTrue(history_payload['success'])
        self.assertGreaterEqual(len(history_payload['versions']), 2)

        compare_response = self.client.get(
            f'/api/repository/{self.repo_id}/compare',
            params={
                'from_commit': self.commit_old,
                'to_commit': self.commit_new,
                'path': 'src/sample.txt'
            }
        )
        self.assertEqual(compare_response.status_code, 200)
        compare_payload = compare_response.json()
        self.assertTrue(compare_payload['success'])
        self.assertEqual(len(compare_payload['files']), 1)
        self.assertFalse(compare_payload['files'][0]['is_binary'])
        self.assertGreater(len(compare_payload['files'][0]['diff']['rows']), 0)

    def test_file_rollback_requires_auth(self):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/rollback/file',
            json={
                'path': 'src/sample.txt',
                'target_commit_id': self.commit_old,
                'branch': 'main'
            }
        )
        self.assertEqual(response.status_code, 401)

    def test_file_rollback_creates_new_commit(self):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/rollback/file',
            headers={'Authorization': f'Bearer {self.auth_token}'},
            json={
                'path': 'src/sample.txt',
                'target_commit_id': self.commit_old,
                'branch': 'main'
            }
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertIn('new_commit_id', payload)

        files_after = self.client.get(f'/api/repository/{self.repo_id}/files', params={'branch': 'main'})
        self.assertEqual(files_after.status_code, 200)
        after_payload = files_after.json()
        self.assertEqual(after_payload['files']['src/sample.txt']['content'], 'line1\nline2\n')

    def test_file_history_integrity_with_rapid_edits_and_restores(self):
        db = self.TestSessionLocal()
        try:
            repo_name = f'rapid_history_{int(datetime.utcnow().timestamp())}'
            repo = RepositoryCRUD.create_repository(db, 'version_admin', repo_name, 'Rapid version history test')
            repo_id = repo.id

            contents = [
                b'first\n',
                b'second\n',
                b'third\n',
                b'fourth\n',
            ]

            parent = None
            created_commits = []
            for idx, content in enumerate(contents, start=1):
                commit_id = hashlib.sha1(f'{repo_id}:rapid:{idx}:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
                commit_data = {
                    'id': commit_id,
                    'repository_id': repo_id,
                    'author': 'version_admin',
                    'message': f'rapid edit {idx}',
                    'files': {
                        'src/rapid.txt': base64.b64encode(content).decode('utf-8')
                    },
                    'timestamp': datetime.utcnow().isoformat()
                }
                if parent:
                    commit_data['parent'] = parent
                    commit_data['parents'] = [parent]

                created = CommitCRUD.create_commit(db, commit_data)
                BranchCRUD.update_branch_head(db, repo_id, 'main', created.id)
                created_commits.append(created.id)
                parent = created.id
        finally:
            db.close()

        # Restore to an older version twice to verify immutable accumulation.
        restore_targets = [created_commits[1], created_commits[0]]
        for target in restore_targets:
            rollback_response = self.client.post(
                f'/api/repository/{repo_id}/rollback/file',
                headers={'Authorization': f'Bearer {self.auth_token}'},
                json={
                    'path': 'src/rapid.txt',
                    'target_commit_id': target,
                    'branch': 'main'
                }
            )
            self.assertEqual(rollback_response.status_code, 200)
            rollback_payload = rollback_response.json()
            self.assertTrue(rollback_payload['success'])
            self.assertIn('new_commit_id', rollback_payload)

        history_response = self.client.get(
            f'/api/repository/{repo_id}/file-history',
            params={'path': 'src/rapid.txt', 'branch': 'main', 'limit': 50}
        )
        self.assertEqual(history_response.status_code, 200)
        history_payload = history_response.json()
        self.assertTrue(history_payload['success'])

        versions = history_payload['versions']
        self.assertGreaterEqual(len(versions), len(contents))

        # Ensure no adjacent duplicate snapshots leak into history.
        for idx in range(len(versions) - 1):
            self.assertNotEqual(versions[idx]['file_hash'], versions[idx + 1]['file_hash'])

        files_after = self.client.get(f'/api/repository/{repo_id}/files', params={'branch': 'main'})
        self.assertEqual(files_after.status_code, 200)
        latest_content = files_after.json()['files']['src/rapid.txt']['content']
        self.assertEqual(latest_content, 'first\n')

    def test_file_history_cursor_pagination_beyond_500(self):
        db = self.TestSessionLocal()
        try:
            repo_name = f'pagination_repo_{int(datetime.utcnow().timestamp())}'
            repo = RepositoryCRUD.create_repository(db, 'version_admin', repo_name, 'Pagination version history test')
            repo_id = repo.id

            parent = None
            for idx in range(520):
                content = f'line-{idx}\n'.encode('utf-8')
                commit_id = hashlib.sha1(f'{repo_id}:page:{idx}:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
                commit_data = {
                    'id': commit_id,
                    'repository_id': repo_id,
                    'author': 'version_admin',
                    'message': f'page edit {idx}',
                    'files': {
                        'src/paginated.txt': base64.b64encode(content).decode('utf-8')
                    },
                    'timestamp': datetime.utcnow().isoformat()
                }
                if parent:
                    commit_data['parent'] = parent
                    commit_data['parents'] = [parent]
                created = CommitCRUD.create_commit(db, commit_data)
                BranchCRUD.update_branch_head(db, repo_id, 'main', created.id)
                parent = created.id
        finally:
            db.close()

        page_1 = self.client.get(
            f'/api/repository/{repo_id}/file-history',
            params={'path': 'src/paginated.txt', 'limit': 120}
        )
        self.assertEqual(page_1.status_code, 200)
        payload_1 = page_1.json()
        self.assertTrue(payload_1['success'])
        self.assertEqual(len(payload_1['versions']), 120)
        self.assertTrue(payload_1['has_more'])
        self.assertIsNotNone(payload_1['next_cursor'])

        total = len(payload_1['versions'])
        cursor = payload_1['next_cursor']
        while cursor:
            page = self.client.get(
                f'/api/repository/{repo_id}/file-history',
                params={'path': 'src/paginated.txt', 'limit': 120, 'cursor': cursor}
            )
            self.assertEqual(page.status_code, 200)
            payload = page.json()
            total += len(payload['versions'])
            cursor = payload.get('next_cursor')

        self.assertGreaterEqual(total, 520)

    def test_file_rollback_head_mismatch_returns_409(self):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/rollback/file',
            headers={'Authorization': f'Bearer {self.auth_token}'},
            json={
                'path': 'src/sample.txt',
                'target_commit_id': self.commit_old,
                'branch': 'main',
                'expected_head_commit_id': 'f' * 40
            }
        )
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload['detail']['code'], 'HEAD_MISMATCH')

    def test_follow_renames_in_file_history(self):
        db = self.TestSessionLocal()
        try:
            repo_name = f'rename_repo_{int(datetime.utcnow().timestamp())}'
            repo = RepositoryCRUD.create_repository(db, 'version_admin', repo_name, 'Rename lineage test repo')
            repo_id = repo.id

            c1 = CommitCRUD.create_commit(db, {
                'id': hashlib.sha1(f'{repo_id}:rename:1'.encode('utf-8')).hexdigest(),
                'repository_id': repo_id,
                'author': 'version_admin',
                'message': 'add old path',
                'files': {
                    'src/old_name.txt': base64.b64encode(b'constant-content\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo_id, 'main', c1.id)

            c2 = CommitCRUD.create_commit(db, {
                'id': hashlib.sha1(f'{repo_id}:rename:2'.encode('utf-8')).hexdigest(),
                'repository_id': repo_id,
                'author': 'version_admin',
                'parent': c1.id,
                'parents': [c1.id],
                'message': 'rename file path',
                'files': {
                    'src/new_name.txt': base64.b64encode(b'constant-content\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo_id, 'main', c2.id)
        finally:
            db.close()

        without_lineage = self.client.get(
            f'/api/repository/{repo_id}/file-history',
            params={'path': 'src/new_name.txt', 'follow_renames': 'false'}
        )
        self.assertEqual(without_lineage.status_code, 200)
        without_payload = without_lineage.json()
        self.assertGreaterEqual(len(without_payload['versions']), 1)

        with_lineage = self.client.get(
            f'/api/repository/{repo_id}/file-history',
            params={'path': 'src/new_name.txt', 'follow_renames': 'true'}
        )
        self.assertEqual(with_lineage.status_code, 200)
        with_payload = with_lineage.json()
        self.assertGreaterEqual(len(with_payload['versions']), 2)
        observed_paths = {item.get('observed_path') for item in with_payload['versions']}
        self.assertIn('src/old_name.txt', observed_paths)
        self.assertIn('src/new_name.txt', observed_paths)

    def test_merge_conflict_returns_structured_payload(self):
        db = self.TestSessionLocal()
        try:
            repo_name = f'merge_conflict_repo_{int(datetime.utcnow().timestamp())}'
            repo = RepositoryCRUD.create_repository(db, 'version_admin', repo_name, 'Merge conflict test repo')
            repo_id = repo.id

            base_commit = CommitCRUD.create_commit(db, {
                'id': hashlib.sha1(f'{repo_id}:merge:base'.encode('utf-8')).hexdigest(),
                'repository_id': repo_id,
                'author': 'version_admin',
                'message': 'base',
                'files': {'src/conflict.txt': base64.b64encode(b'base\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo_id, 'main', base_commit.id)
            BranchCRUD.create_branch(db, repo_id, 'feature_conflict', head_commit_id=base_commit.id)

            main_commit = CommitCRUD.create_commit(db, {
                'id': hashlib.sha1(f'{repo_id}:merge:main'.encode('utf-8')).hexdigest(),
                'repository_id': repo_id,
                'author': 'version_admin',
                'parent': base_commit.id,
                'parents': [base_commit.id],
                'message': 'main change',
                'files': {'src/conflict.txt': base64.b64encode(b'main-change\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo_id, 'main', main_commit.id)

            feature_commit = CommitCRUD.create_commit(db, {
                'id': hashlib.sha1(f'{repo_id}:merge:feature'.encode('utf-8')).hexdigest(),
                'repository_id': repo_id,
                'author': 'version_admin',
                'parent': base_commit.id,
                'parents': [base_commit.id],
                'message': 'feature change',
                'files': {'src/conflict.txt': base64.b64encode(b'feature-change\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo_id, 'feature_conflict', feature_commit.id)
        finally:
            db.close()

        pr_response = self.client.post(
            f'/api/repository/{repo_id}/pull-requests',
            headers={'Authorization': f'Bearer {self.auth_token}'},
            json={
                'title': 'Conflict PR',
                'description': 'Expect conflict',
                'source_branch': 'feature_conflict',
                'target_branch': 'main'
            }
        )
        self.assertEqual(pr_response.status_code, 200)
        pr_id = pr_response.json()['pull_request']['id']

        merge_response = self.client.post(
            f'/api/repository/{repo_id}/pull-requests/{pr_id}/merge',
            headers={'Authorization': f'Bearer {self.auth_token}'},
            json={'expected_head_commit_id': hashlib.sha1(f'{repo_id}:merge:main'.encode('utf-8')).hexdigest()}
        )
        self.assertEqual(merge_response.status_code, 409)
        merge_payload = merge_response.json()
        self.assertEqual(merge_payload['code'], 'MERGE_CONFLICT')
        self.assertEqual(merge_payload['status'], 'conflicts')
        self.assertGreater(len(merge_payload['conflicts']), 0)


if __name__ == '__main__':
    unittest.main()
