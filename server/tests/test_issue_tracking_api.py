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


class IssueTrackingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, db_path = tempfile.mkstemp(prefix='foxnest_issue_test_', suffix='.db')
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
                username='issue_admin',
                role='team_lead',
                password_hash=password_hash,
                email='admin@example.com'
            )
            repo = RepositoryCRUD.create_repository(db, 'issue_admin', 'issue_repo', 'Issue tracking test repo')
            cls.repo_id = repo.id

            # Ensure there is at least one commit on main
            c1 = CommitCRUD.create_commit(db, {
                'id': 'c' * 40,
                'repository_id': repo.id,
                'author': 'issue_admin',
                'message': 'initial commit',
                'files': {
                    'README.md': base64.b64encode(b'hello\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, repo.id, 'main', c1.id)
            cls.main_head = c1.id

            cls.auth_token = server_module.create_access_token('issue_admin', 'team_lead')
        finally:
            db.close()

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self.auth_token}'}

    def test_create_and_get_issue(self):
        create = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(),
            json={
                'title': 'Bug: something is broken',
                'description': 'Steps to reproduce @issue_admin',
                'issue_type': 'bug',
                'priority': 'high'
            }
        )
        self.assertEqual(create.status_code, 200)
        payload = create.json()
        self.assertTrue(payload['success'])
        issue_number = payload['issue']['number']

        detail = self.client.get(
            f'/api/repository/{self.repo_id}/issues/{issue_number}',
            headers=self._auth_headers(),
        )
        self.assertEqual(detail.status_code, 200)
        issue = detail.json()['issue']
        self.assertEqual(issue['number'], issue_number)
        self.assertEqual(issue['status'], 'open')
        self.assertEqual(issue['issue_type'], 'bug')

    def test_commit_fixes_issue_on_default_branch(self):
        create = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(),
            json={
                'title': 'Task: close me via commit',
                'description': 'Will be closed by Fixes syntax',
                'issue_type': 'task',
                'priority': 'medium'
            }
        )
        issue_number = create.json()['issue']['number']

        commit_id = hashlib.sha1(f'{self.repo_id}:fix:{issue_number}:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
        push = self.client.post(
            f'/api/repository/{self.repo_id}/push',
            json={
                'commit': {
                    'id': commit_id,
                    'author': 'issue_admin',
                    'message': f'Fixes #{issue_number}',
                    'parent': self.main_head,
                    'parents': [self.main_head],
                    'files': {'README.md': base64.b64encode(b'hello again\n').decode('utf-8')},
                    'timestamp': datetime.utcnow().isoformat()
                },
                'branch': 'main',
                'pusher': 'issue_admin'
            },
            # Push now requires authentication; this call was the only one in the file
            # without it, and previously succeeded only because the endpoint was open.
            headers=self._auth_headers(),
        )
        self.assertEqual(push.status_code, 200)
        # Update cached main head for subsequent tests.
        self.__class__.main_head = commit_id

        detail = self.client.get(
            f'/api/repository/{self.repo_id}/issues/{issue_number}',
            headers=self._auth_headers(),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['issue']['status'], 'closed')

    def test_pr_merge_auto_closes_issue(self):
        # Create issue
        create = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(),
            json={
                'title': 'Feature: auto close on merge',
                'description': 'Should close when PR is merged',
                'issue_type': 'feature',
                'priority': 'low'
            }
        )
        issue_number = create.json()['issue']['number']

        # Prepare a branch with a new commit.
        db = self.TestSessionLocal()
        try:
            # Always read current main head (prior tests may have advanced it).
            main_branch = BranchCRUD.get_branch(db, self.repo_id, 'main')
            current_main_head = main_branch.head_commit_id

            BranchCRUD.create_branch(db, self.repo_id, f'fix/issue-{issue_number}', head_commit_id=current_main_head)
            new_commit_id = hashlib.sha1(f'{self.repo_id}:pr:{issue_number}:{datetime.utcnow().isoformat()}'.encode('utf-8')).hexdigest()
            created = CommitCRUD.create_commit(db, {
                'id': new_commit_id,
                'repository_id': self.repo_id,
                'author': 'issue_admin',
                'parent': current_main_head,
                'parents': [current_main_head],
                'message': f'Implement feature (Refs #{issue_number})',
                'files': {'src/feature.txt': base64.b64encode(b'feature\n').decode('utf-8')},
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, self.repo_id, f'fix/issue-{issue_number}', created.id)

            main_branch = BranchCRUD.get_branch(db, self.repo_id, 'main')
            main_head = main_branch.head_commit_id
        finally:
            db.close()

        # Create PR referencing the issue
        pr_res = self.client.post(
            f'/api/repository/{self.repo_id}/pull-requests',
            headers=self._auth_headers(),
            json={
                'title': f'Fixes #{issue_number}: implement feature',
                'description': 'Please merge',
                'source_branch': f'fix/issue-{issue_number}',
                'target_branch': 'main'
            }
        )
        self.assertEqual(pr_res.status_code, 200)
        pr_id = pr_res.json()['pull_request']['id']

        merge = self.client.post(
            f'/api/repository/{self.repo_id}/pull-requests/{pr_id}/merge',
            headers=self._auth_headers(),
            json={'expected_head_commit_id': main_head}
        )
        self.assertEqual(merge.status_code, 200)

        detail = self.client.get(
            f'/api/repository/{self.repo_id}/issues/{issue_number}',
            headers=self._auth_headers(),
        )
        self.assertEqual(detail.status_code, 200)
        issue = detail.json()['issue']
        self.assertEqual(issue['status'], 'closed')
        pr_links = issue.get('links', {}).get('pull_requests', [])
        self.assertTrue(any(p.get('id') == pr_id for p in pr_links))


if __name__ == '__main__':
    unittest.main()

