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
from database.crud import (
    UserCRUD, RepositoryCRUD, CommitCRUD, BranchCRUD,
    IssueAccessRequestCRUD, UserPermissionCRUD, NotificationCRUD
)


class IssueAccessRequestApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, db_path = tempfile.mkstemp(prefix='foxnest_access_req_test_', suffix='.db')
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
            # Create users
            password_hash = server_module.hash_password('Password123!')
            
            # Admin (repo owner) - team lead
            cls.admin_user = UserCRUD.create_user(
                db,
                username='admin_user',
                role='team_lead',
                password_hash=password_hash,
                email='admin@example.com',
                full_name='Admin User'
            )
            
            # Developer - needs access
            cls.dev_user = UserCRUD.create_user(
                db,
                username='dev_user',
                role='developer',
                password_hash=password_hash,
                email='dev@example.com',
                full_name='Developer User',
                team_lead_id=cls.admin_user.id
            )
            cls.dev_user_id = cls.dev_user.id
            
            # Another developer
            cls.dev_user2 = UserCRUD.create_user(
                db,
                username='dev_user2',
                role='developer',
                password_hash=password_hash,
                email='dev2@example.com',
                full_name='Developer User 2',
                team_lead_id=cls.admin_user.id
            )
            cls.dev_user2_id = cls.dev_user2.id
            
            # Create repository owned by admin
            cls.repository = RepositoryCRUD.create_repository(
                db, 'admin_user', 'test_repo', 'Test Repository'
            )
            cls.repo_id = cls.repository.id
            
            # Create initial commit on main branch
            c1 = CommitCRUD.create_commit(db, {
                'id': 'c' * 40,
                'repository_id': cls.repo_id,
                'author': 'admin_user',
                'message': 'Initial commit',
                'files': {
                    'README.md': base64.b64encode(b'# Test Repo\n').decode('utf-8')
                },
                'timestamp': datetime.utcnow().isoformat()
            })
            BranchCRUD.update_branch_head(db, cls.repo_id, 'main', c1.id)
            
            # Generate tokens
            cls.admin_token = server_module.create_access_token('admin_user', 'team_lead')
            cls.dev_token = server_module.create_access_token('dev_user', 'developer')
            cls.dev_token2 = server_module.create_access_token('dev_user2', 'developer')
            
        finally:
            db.close()

    def _auth_headers(self, token):
        return {'Authorization': f'Bearer {token}'}

    def test_create_issue_with_access_request(self):
        """Test creating an issue with @mention triggers access request"""
        # Create issue by admin mentioning dev_user
        create_response = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(self.admin_token),
            json={
                'title': 'Add feature X',
                'description': '@dev_user please implement this feature',
                'issue_type': 'feature',
                'priority': 'high'
            }
        )
        
        self.assertEqual(create_response.status_code, 200)
        issue_data = create_response.json()
        self.assertTrue(issue_data['success'])
        issue = issue_data['issue']
        self.assertIn('number', issue)
        
        # Verify access request was created
        db = self.TestSessionLocal()
        try:
            requests = IssueAccessRequestCRUD.list_pending_requests(
                db, repo_id=self.repo_id, status='pending'
            )
            self.assertTrue(any(r.requested_user.username == 'dev_user' for r in requests))
            self.assertTrue(all(r.status == 'pending' for r in requests if r.requested_user.username == 'dev_user'))
        finally:
            db.close()

    def test_create_issue_assign_without_access_creates_request(self):
        """Test creating an issue with an assignee lacking access triggers access request"""
        create_response = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(self.admin_token),
            json={
                'title': 'Assign work to dev_user2',
                'description': 'Please take ownership of this task',
                'assigned_to': '@dev_user2',
                'issue_type': 'task',
                'priority': 'medium'
            }
        )
        self.assertEqual(create_response.status_code, 200)
        issue_data = create_response.json()
        self.assertTrue(issue_data['success'])

        db = self.TestSessionLocal()
        try:
            requests = [r for r in IssueAccessRequestCRUD.list_user_requests(db, self.dev_user2_id, status='pending') if r.repository_id == self.repo_id]
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].requested_user.username, 'dev_user2')
            self.assertEqual(requests[0].status, 'pending')
        finally:
            db.close()

    def test_multiple_mentions_create_multiple_requests(self):
        """Test multiple @mentions create multiple access requests"""
        create_response = self.client.post(
            f'/api/repository/{self.repo_id}/issues',
            headers=self._auth_headers(self.admin_token),
            json={
                'title': 'Large feature',
                'description': '@dev_user and @dev_user2 should work on this',
                'issue_type': 'feature',
                'priority': 'high'
            }
        )
        
        self.assertEqual(create_response.status_code, 200)
        
        # Verify multiple access requests were created
        db = self.TestSessionLocal()
        try:
            all_requests = IssueAccessRequestCRUD.list_pending_requests(
                db, repo_id=self.repo_id, status='pending'
            )
            # Should have 2 requests (one for each developer)
            self.assertGreaterEqual(len(all_requests), 2)
        finally:
            db.close()

    def test_approve_access_request(self):
        """Test approving an access request grants permission"""
        db = self.TestSessionLocal()
        try:
            # Create issue by admin to trigger access request
            issue_response = self.client.post(
                f'/api/repository/{self.repo_id}/issues',
                headers=self._auth_headers(self.admin_token),
                json={
                    'title': 'Test approval flow',
                    'description': '@dev_user please fix this',
                    'issue_type': 'bug',
                    'priority': 'critical'
                }
            )
            self.assertEqual(issue_response.status_code, 200)
            
            # Get pending request
            requests = IssueAccessRequestCRUD.list_pending_requests(
                db, repo_id=self.repo_id, status='pending'
            )
            self.assertGreater(len(requests), 0)
            req = requests[-1]  # Get latest
            
            # Approve request
            approve_response = self.client.post(
                f'/api/access-requests/{req.id}/approve',
                headers=self._auth_headers(self.admin_token),
                json={'comment': 'Approved'}
            )
            
            self.assertEqual(approve_response.status_code, 200)
            approve_data = approve_response.json()
            self.assertTrue(approve_data['success'])
            self.assertEqual(approve_data['permission']['permission_level'], 'write')
            
            # Verify permission was granted
            perm = UserPermissionCRUD.get_user_permission(
                db, 'dev_user', self.repo_id
            )
            self.assertIsNotNone(perm)
            self.assertEqual(perm.permission_level, 'write')
            
        finally:
            db.close()

    def test_deny_access_request(self):
        """Test denying an access request"""
        db = self.TestSessionLocal()
        try:
            # Create issue to trigger access request
            issue_response = self.client.post(
                f'/api/repository/{self.repo_id}/issues',
                headers=self._auth_headers(self.admin_token),
                json={
                    'title': 'Test denial flow',
                    'description': '@dev_user2 try but might be denied',
                    'issue_type': 'task',
                    'priority': 'medium'
                }
            )
            self.assertEqual(issue_response.status_code, 200)
            
            # Get pending request for dev_user2
            requests = IssueAccessRequestCRUD.list_user_requests(
                db, user_id=self.dev_user2.id, status='pending'
            )
            self.assertGreater(len(requests), 0)
            req = requests[0]
            
            # Deny request
            deny_response = self.client.post(
                f'/api/access-requests/{req.id}/deny',
                headers=self._auth_headers(self.admin_token),
                json={'comment': 'Already working on this'}
            )
            
            self.assertEqual(deny_response.status_code, 200)
            deny_data = deny_response.json()
            self.assertTrue(deny_data['success'])
            self.assertEqual(deny_data['request_status']['status'], 'denied')
            
            # Verify permission was NOT granted
            perm = UserPermissionCRUD.get_user_permission(
                db, 'dev_user2', self.repo_id
            )
            self.assertIsNone(perm)
            
        finally:
            db.close()

    def test_list_pending_requests_as_admin(self):
        """Test admin can list pending access requests"""
        # Create a few issues to generate requests
        for i in range(2):
            self.client.post(
                f'/api/repository/{self.repo_id}/issues',
                headers=self._auth_headers(self.admin_token),
                json={
                    'title': f'Issue {i}',
                    'description': f'@dev_user issue number {i}',
                    'issue_type': 'task',
                    'priority': 'medium'
                }
            )
        
        # List pending requests
        list_response = self.client.get(
            f'/api/access-requests/pending?repo_id={self.repo_id}',
            headers=self._auth_headers(self.admin_token)
        )
        
        self.assertEqual(list_response.status_code, 200)
        list_data = list_response.json()
        self.assertTrue(list_data['success'])
        self.assertGreater(len(list_data['access_requests']), 0)

    def test_non_admin_cannot_approve(self):
        """Test that only repo owner can approve requests"""
        db = self.TestSessionLocal()
        try:
            # Create issue to trigger access request
            self.client.post(
                f'/api/repository/{self.repo_id}/issues',
                headers=self._auth_headers(self.admin_token),
                json={
                    'title': 'Permission test',
                    'description': '@dev_user this tests permissions',
                    'issue_type': 'task',
                    'priority': 'medium'
                }
            )
            
            # Get the access request
            requests = IssueAccessRequestCRUD.list_pending_requests(
                db, repo_id=self.repo_id, status='pending'
            )
            req = requests[-1]
            
            # Try to approve as non-owner (dev_user)
            approve_response = self.client.post(
                f'/api/access-requests/{req.id}/approve',
                headers=self._auth_headers(self.dev_token),
                json={'comment': 'Approve'}
            )
            
            # Should fail with 403 Forbidden
            self.assertEqual(approve_response.status_code, 403)
            
        finally:
            db.close()

    def test_no_request_for_repo_owner(self):
        """Test that no access request is created if @mentioned user owns repo"""
        # Create repo owned by dev_user
        db = self.TestSessionLocal()
        try:
            dev_repo = RepositoryCRUD.create_repository(
                db, 'dev_user', 'dev_repo', 'Dev owned repo'
            )
            
            # Create issue with admin mentioning themselves (owner)
            # This should NOT create an access request
            issue_response = self.client.post(
                f'/api/repository/{dev_repo.id}/issues',
                headers=self._auth_headers(self.dev_token),
                json={
                    'title': 'Self reference',
                    'description': '@dev_user fix this',
                    'issue_type': 'bug',
                    'priority': 'high'
                }
            )
            
            self.assertEqual(issue_response.status_code, 200)
            
            # Verify NO access request was created
            requests = IssueAccessRequestCRUD.list_pending_requests(
                db, repo_id=dev_repo.id, status='pending'
            )
            self.assertEqual(len(requests), 0)
            
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
