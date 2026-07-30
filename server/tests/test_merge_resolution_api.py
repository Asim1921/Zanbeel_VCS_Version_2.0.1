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


class MergeResolutionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, db_path = tempfile.mkstemp(prefix='foxnest_merge_test_', suffix='.db')
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
                username='merge_admin',
                role='team_lead',
                password_hash=password_hash,
                email='merge_admin@example.com'
            )
            repo = RepositoryCRUD.create_repository(db, 'merge_admin', 'merge_repo', 'Merge resolution repo')
            cls.repo_id = repo.id

            # base commit on main
            base_id = 'b' * 40
            CommitCRUD.create_commit(db, {
                'id': base_id,
                'repository_id': repo.id,
                'author': 'merge_admin',
                'message': 'base',
                'files': {'conflict.txt': base64.b64encode(b'hello\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'main', base_id)
            cls.base_id = base_id

            # create feature branch at base
            BranchCRUD.create_branch(db, repo.id, 'feature', head_commit_id=base_id)

            # commit on main changes same line
            main_id = hashlib.sha1(f'{repo.id}:main:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
            CommitCRUD.create_commit(db, {
                'id': main_id,
                'repository_id': repo.id,
                'author': 'merge_admin',
                'parents': [base_id],
                'message': 'main change',
                'files': {'conflict.txt': base64.b64encode(b'hello main\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'main', main_id)
            cls.main_id = main_id

            # commit on feature changes same line differently
            feat_id = hashlib.sha1(f'{repo.id}:feature:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
            CommitCRUD.create_commit(db, {
                'id': feat_id,
                'repository_id': repo.id,
                'author': 'merge_admin',
                'parents': [base_id],
                'message': 'feature change',
                'files': {'conflict.txt': base64.b64encode(b'hello feature\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'feature', feat_id)
            cls.feat_id = feat_id

            cls.auth_token = server_module.create_access_token('merge_admin', 'team_lead')
        finally:
            db.close()

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self.auth_token}'}

    def test_merge_conflict_session_and_resolve(self):
        # Create PR
        pr_res = self.client.post(
            f'/api/repository/{self.repo_id}/pull-requests',
            headers=self._auth_headers(),
            json={
                'title': 'Merge feature',
                'description': 'Test merge conflicts',
                'source_branch': 'feature',
                'target_branch': 'main'
            }
        )
        self.assertEqual(pr_res.status_code, 200)
        pr_id = pr_res.json()['pull_request']['id']

        # Attempt merge -> conflict with session_id
        merge_res = self.client.post(
            f'/api/repository/{self.repo_id}/pull-requests/{pr_id}/merge',
            headers=self._auth_headers(),
            json={'expected_head_commit_id': self.main_id}
        )
        self.assertEqual(merge_res.status_code, 409)
        merge_payload = merge_res.json()
        self.assertEqual(merge_payload.get('code'), 'MERGE_CONFLICT')
        session_id = merge_payload.get('session_id')
        self.assertTrue(session_id)

        # Fetch conflict bundle
        bundle = self.client.get(
            f'/api/repository/{self.repo_id}/pull-requests/{pr_id}/merge-conflicts/{session_id}',
            headers=self._auth_headers()
        )
        self.assertEqual(bundle.status_code, 200)
        files = bundle.json().get('files', [])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['path'], 'conflict.txt')

        # Resolve by choosing "ours" (main) explicitly
        resolved_content = base64.b64encode(b'hello main\n').decode('utf-8')
        resolve = self.client.post(
            f'/api/repository/{self.repo_id}/pull-requests/{pr_id}/merge-resolve/{session_id}',
            headers=self._auth_headers(),
            json={
                'expected_head_commit_id': self.main_id,
                'resolutions': {
                    'conflict.txt': resolved_content
                }
            }
        )
        self.assertEqual(resolve.status_code, 200)
        self.assertTrue(resolve.json().get('success'))
        self.assertEqual(resolve.json().get('status'), 'merged')


if __name__ == '__main__':
    unittest.main()

