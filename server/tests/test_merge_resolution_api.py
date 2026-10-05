"""The pull request conflict workflow over HTTP, end to end, the way GitHub runs it.

Merge refuses with MERGE_CONFLICT -> the resolver opens a session -> decisions are saved
per file -> "Commit merge" writes the resolution to the pull request's own branch -> the
pull request then merges through the normal path, webhook and all.
"""
import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

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
from database.crud import UserCRUD, RepositoryCRUD, CommitCRUD, BranchCRUD, UserPermissionCRUD
from database.models import Branch, PullRequest, Repository
from app.services import merge_conflicts


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('utf-8')


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
        cls._seed_users()

    @classmethod
    def tearDownClass(cls):
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls.original_startup
        cls.client.close()
        cls.engine.dispose()
        if os.path.exists(cls._db_path):
            os.remove(cls._db_path)

    @classmethod
    def _seed_users(cls):
        db = cls.TestSessionLocal()
        try:
            password_hash = server_module.hash_password('Password123!')
            UserCRUD.create_user(db, username='merge_admin', role='team_lead',
                                 password_hash=password_hash, email='merge_admin@example.com')
            UserCRUD.create_user(db, username='reader', role='developer',
                                 password_hash=password_hash, email='reader@example.com')
            UserCRUD.create_user(db, username='contrib', role='developer',
                                 password_hash=password_hash, email='contrib@example.com')
        finally:
            db.close()
        cls.admin_token = server_module.create_access_token('merge_admin', 'team_lead')
        cls.reader_token = server_module.create_access_token('reader', 'developer')

    def setUp(self):
        merge_conflicts._MERGEABILITY_CACHE.clear()

    # --- fixtures ------------------------------------------------------------------

    def _headers(self, token=None):
        return {'Authorization': f'Bearer {token or self.admin_token}'}

    def _commit(self, db, repo_id, label, files, parents=()):
        commit_id = hashlib.sha1(
            f'{repo_id}:{label}:{datetime.utcnow().isoformat()}'.encode()).hexdigest()
        CommitCRUD.create_commit(db, {
            'id': commit_id,
            'repository_id': repo_id,
            'author': 'merge_admin',
            'parents': list(parents),
            'message': label,
            'files': {path: b64(data) for path, data in files.items()},
            'timestamp': datetime.utcnow().isoformat(),
        })
        return commit_id

    def _conflicted_repo(self, name):
        """main and feature both edit line 2 of conflict.txt; line 4 differs cleanly."""
        db = self.TestSessionLocal()
        try:
            repo = RepositoryCRUD.create_repository(db, 'merge_admin', name, 'conflicts')
            base = self._commit(db, repo.id, 'base', {'conflict.txt': b'1\n2\n3\n4\n'})
            BranchCRUD.update_branch_head(db, repo.id, 'main', base)
            BranchCRUD.create_branch(db, repo.id, 'feature', head_commit_id=base)
            main = self._commit(db, repo.id, 'main', {'conflict.txt': b'1\nmain\n3\n4\n'}, [base])
            BranchCRUD.update_branch_head(db, repo.id, 'main', main)
            feat = self._commit(db, repo.id, 'feat', {'conflict.txt': b'1\nfeature\n3\nfour\n'},
                                [base])
            BranchCRUD.update_branch_head(db, repo.id, 'feature', feat)
            return repo.id, main, feat
        finally:
            db.close()

    def _open_pr(self, repo_id):
        res = self.client.post(f'/api/repository/{repo_id}/pull-requests',
                               headers=self._headers(),
                               json={'title': 'Merge feature', 'description': 'x',
                                     'source_branch': 'feature', 'target_branch': 'main'})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()['pull_request']['id']

    # --- the whole flow ----------------------------------------------------------------

    def test_conflict_resolution_flow_matches_github(self):
        repo_id, main_id, feat_id = self._conflicted_repo('flow_repo')
        pr_id = self._open_pr(repo_id)
        base_url = f'/api/repository/{repo_id}/pull-requests/{pr_id}'

        # The PR reports its conflicts before anyone presses Merge.
        state = self.client.get(f'{base_url}/mergeability', headers=self._headers()).json()
        self.assertEqual(state['state'], 'conflicts')
        self.assertEqual(state['conflicts'], ['conflict.txt'])
        self.assertTrue(state['can_resolve'])

        merge_res = self.client.post(f'{base_url}/merge', headers=self._headers(),
                                     json={'expected_head_commit_id': main_id})
        self.assertEqual(merge_res.status_code, 409)
        self.assertEqual(merge_res.json()['code'], 'MERGE_CONFLICT')

        opened = self.client.post(f'{base_url}/conflicts', headers=self._headers())
        self.assertEqual(opened.status_code, 200, opened.text)
        bundle = opened.json()
        session_id = bundle['session']['id']
        self.assertEqual(bundle['session']['commit_branch'], 'feature')
        entry = bundle['files'][0]
        self.assertEqual(entry['conflict_count'], 1)
        suggested = base64.b64decode(entry['suggested_b64']).decode()
        # Line 4 merged cleanly; only line 2 is in conflict.
        self.assertTrue(suggested.endswith('3\nfour\n'))
        self.assertIn('<<<<<<< main', suggested)

        # Leaving the markers in is refused, in the shared error shape.
        refused = self.client.post(f'{base_url}/conflicts/{session_id}/resolve',
                                   headers=self._headers(),
                                   json={'resolutions': {'conflict.txt': entry['suggested_b64']}})
        self.assertEqual(refused.status_code, 409)
        body = refused.json()
        self.assertEqual((body['success'], body['code']), (False, 'UNRESOLVED_MARKERS'))
        self.assertIn('detail', body)

        # Save the decision as a draft; the session listing shows the progress.
        saved = self.client.put(f'{base_url}/conflicts/{session_id}/files',
                                headers=self._headers(),
                                json={'path': 'conflict.txt',
                                      'content_b64': b64(b'1\nmain and feature\n3\nfour\n')})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['unresolved'], 0)
        listed = self.client.get(f'{base_url}/conflicts', headers=self._headers()).json()
        self.assertEqual(listed['sessions'][0]['resolved'], 1)

        # Commit merge: nothing more to send, the draft is the decision.
        resolved = self.client.post(f'{base_url}/conflicts/{session_id}/resolve',
                                    headers=self._headers(),
                                    json={'expected_head_commit_id': feat_id})
        self.assertEqual(resolved.status_code, 200, resolved.text)
        result = resolved.json()
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['commit_branch'], 'feature')

        db = self.TestSessionLocal()
        try:
            self.assertEqual(BranchCRUD.get_branch(db, repo_id, 'main').head_commit_id, main_id)
            self.assertEqual(BranchCRUD.get_branch(db, repo_id, 'feature').head_commit_id,
                             result['merge_commit_id'])
        finally:
            db.close()

        # The pull request is still open and now merges through the normal path.
        state = self.client.get(f'{base_url}/mergeability', headers=self._headers()).json()
        self.assertEqual(state['state'], 'clean')
        with mock.patch('app.services.webhooks.dispatch') as dispatch:
            merged = self.client.post(f'{base_url}/merge', headers=self._headers(),
                                      json={'expected_head_commit_id': main_id})
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(merged.json()['status'], 'merged')
        self.assertTrue(dispatch.called, 'the normal merge path sends the merge webhook')

    def test_branch_conflict_endpoints_share_the_error_shape(self):
        repo_id, _main, _feat = self._conflicted_repo('branch_shape_repo')
        opened = self.client.post(f'/api/repository/{repo_id}/branches/conflicts',
                                  headers=self._headers(),
                                  json={'source_branch': 'feature', 'target_branch': 'main'})
        self.assertEqual(opened.status_code, 200, opened.text)
        session_id = opened.json()['session']['id']
        self.assertEqual(opened.json()['session']['commit_branch'], 'main')

        incomplete = self.client.post(
            f'/api/repository/{repo_id}/branches/conflicts/{session_id}/resolve',
            headers=self._headers(), json={})
        self.assertEqual(incomplete.status_code, 400)
        self.assertEqual(incomplete.json()['code'], 'INCOMPLETE')
        self.assertIsInstance(incomplete.json()['detail'], str)

        done = self.client.post(
            f'/api/repository/{repo_id}/branches/conflicts/{session_id}/resolve',
            headers=self._headers(),
            json={'resolutions': {'conflict.txt': b64(b'1\nboth\n3\nfour\n')}})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()['status'], 'merged')

    def test_someone_who_cannot_push_to_the_branch_cannot_resolve(self):
        repo_id, _main, _feat = self._conflicted_repo('reader_repo')
        pr_id = self._open_pr(repo_id)
        db = self.TestSessionLocal()
        try:
            UserPermissionCRUD.create_permission(db, 'reader', repo_id, 'read', 'merge_admin')
        finally:
            db.close()

        url = f'/api/repository/{repo_id}/pull-requests/{pr_id}'
        state = self.client.get(f'{url}/mergeability',
                                headers=self._headers(self.reader_token)).json()
        self.assertEqual(state['state'], 'conflicts')
        self.assertFalse(state['can_resolve'])

        refused = self.client.post(f'{url}/conflicts', headers=self._headers(self.reader_token))
        self.assertEqual(refused.status_code, 403)
        self.assertIn('cannot push', refused.json()['detail'])

    def test_the_cli_resolves_a_pull_request_against_the_real_api(self):
        """`fox pr resolve` driven through the actual endpoints, not a stand-in."""
        import contextlib
        import io
        import shutil
        from pathlib import Path

        repo_id, main_id, feat_id = self._conflicted_repo('cli_repo')
        pr_id = self._open_pr(repo_id)

        fox_path = Path(SERVER_ROOT).parent / 'client' / 'client1' / 'fox.py'
        fox_spec = importlib.util.spec_from_file_location('fox_cli_contract', fox_path)
        fox_module = importlib.util.module_from_spec(fox_spec)
        fox_spec.loader.exec_module(fox_module)
        FoxClient = fox_module.FoxClient

        client, headers = self.client, self._headers()

        def post(_self, url, json=None, params=None, timeout=30):
            return client.post(url, json=json, params=params, headers=headers)

        def get(_self, url, params=None, timeout=30):
            return client.get(url, params=params, headers=headers)

        workdir = Path(tempfile.mkdtemp(prefix='fox_cli_contract_'))
        cwd = os.getcwd()
        try:
            os.chdir(workdir)
            with mock.patch.object(FoxClient, '_repo_api',
                                   lambda _self, suffix: f'http://testserver/api/repository/{repo_id}{suffix}'), \
                    mock.patch.object(FoxClient, '_authed_post', post), \
                    mock.patch.object(FoxClient, '_authed_get', get), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                FoxClient().init(username='merge_admin', repo_name='cli_repo')
                self.assertTrue(FoxClient().pr_resolve(pr_id), out.getvalue())

                resolved = next(Path('.fox/merge-conflicts').rglob('conflict.txt.resolved'))
                self.assertIn(b'<<<<<<< main', resolved.read_bytes())
                resolved.write_bytes(b'1\nresolved from the cli\n3\nfour\n')

                self.assertTrue(FoxClient().pr_resolve(pr_id, submit=True), out.getvalue())
        finally:
            os.chdir(cwd)
            shutil.rmtree(workdir, ignore_errors=True)

        self.assertIn("Committed the resolution to 'feature'", out.getvalue())
        db = self.TestSessionLocal()
        try:
            from app.services.commit_graph import _get_commit_tree
            feature_head = BranchCRUD.get_branch(db, repo_id, 'feature').head_commit_id
            self.assertNotEqual(feature_head, feat_id)
            self.assertEqual(_get_commit_tree(db, feature_head)['conflict.txt'],
                             b'1\nresolved from the cli\n3\nfour\n')
            self.assertEqual(BranchCRUD.get_branch(db, repo_id, 'main').head_commit_id, main_id)
        finally:
            db.close()

    def test_a_fork_pull_request_merges_the_fork_branch(self):
        """main -> main from a fork used to merge upstream's own main and drop the work."""
        db = self.TestSessionLocal()
        try:
            upstream = RepositoryCRUD.create_repository(db, 'merge_admin', 'fork_up', 'up')
            base = self._commit(db, upstream.id, 'base', {'app.txt': b'a\n', 'lib.txt': b'l\n'})
            BranchCRUD.update_branch_head(db, upstream.id, 'main', base)

            fork = Repository(id='forkrepo00000001', name='fork_up',
                              owner_id=UserCRUD.get_user_by_username(db, 'contrib').id,
                              forked_from_id=upstream.id)
            db.add(fork)
            db.commit()
            work = self._commit(db, fork.id, 'fork work',
                                {'app.txt': b'a\n', 'lib.txt': b'fork contribution\n'}, [base])
            db.add(Branch(repository_id=fork.id, name='main', head_commit_id=work))
            pr = PullRequest(repository_id=upstream.id, source_repository_id=fork.id,
                             title='from fork', source_branch='main', target_branch='main',
                             status='open',
                             created_by_id=UserCRUD.get_user_by_username(db, 'contrib').id)
            db.add(pr)
            db.commit()
            upstream_id, pr_id = upstream.id, pr.id
        finally:
            db.close()

        url = f'/api/repository/{upstream_id}/pull-requests/{pr_id}'
        listing = self.client.get(f'/api/repository/{upstream_id}/pull-requests',
                                  headers=self._headers()).json()
        self.assertEqual(listing['pull_requests'][0]['source_repository_id'], 'forkrepo00000001')

        merged = self.client.post(f'{url}/merge', headers=self._headers(), json={})
        self.assertEqual(merged.status_code, 200, merged.text)

        db = self.TestSessionLocal()
        try:
            from app.services.commit_graph import _get_commit_tree
            head = BranchCRUD.get_branch(db, upstream_id, 'main').head_commit_id
            self.assertEqual(_get_commit_tree(db, head)['lib.txt'], b'fork contribution\n')
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
