"""Branch protection, written as the attacks it has to survive.

Section 20.3 of the specification lists the operations that MUST fail. Those are the
interesting tests: a protection feature is only worth having if the alternate routes to
the same mutation are closed too. Before this work there were sixteen ways to move a
branch and two of them consulted a policy, so most of what follows would have passed
straight through.
"""

import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
spec = importlib.util.spec_from_file_location('foxnest_server_module', SERVER_FILE)
server_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server_module)

from database.database import Base
from database.crud import BranchCRUD, CommitCRUD, RepositoryCRUD, UserCRUD
from database.models import Branch, BranchProtectionPolicy, BranchUnlock, RefAuditEvent
from app.services import branch_protection as protection
from app.services import refs


class BranchProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, db_path = tempfile.mkstemp(prefix='foxnest_protect_', suffix='.db')
        os.close(fd)
        cls._db_path = db_path
        cls.engine = create_engine(f"sqlite:///{db_path}")
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        cls.original_startup = list(server_module.app.router.on_startup)
        server_module.app.router.on_startup = []

        def override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        server_module.app.dependency_overrides[server_module.get_db] = override_get_db
        cls.client = TestClient(server_module.app)

    @classmethod
    def tearDownClass(cls):
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls.original_startup
        cls.client.close()
        cls.engine.dispose()
        if os.path.exists(cls._db_path):
            os.remove(cls._db_path)

    # ---------------------------------------------------------------- helpers

    def setUp(self):
        """A fresh repository per test, so a freeze in one cannot leak into another."""
        db = self.Session()
        try:
            unique = hashlib.sha1(self.id().encode()).hexdigest()[:10]
            self.admin = UserCRUD.get_user_by_username(db, 'protect_admin')
            if not self.admin:
                self.admin = UserCRUD.create_user(
                    db, username='protect_admin', role='admin',
                    password_hash=server_module.hash_password('Passw0rd!'),
                    email='admin@example.com',
                )
            self.admin_token = server_module.create_access_token('protect_admin', 'admin')

            repo = RepositoryCRUD.create_repository(
                db, 'protect_admin', f'protected_repo_{unique}', 'protection tests'
            )
            self.repo_id = repo.id

            # Commit ids must be unique per test: the tables are shared across the class.
            def cid(tag):
                return hashlib.sha1(f'{unique}:{tag}'.encode()).hexdigest()

            self.c1 = self._commit(db, repo.id, cid('c1'), {'app.py': b'one\n'}, [])
            self.c2 = self._commit(db, repo.id, cid('c2'), {'app.py': b'two\n'}, [self.c1])
            # A commit on a side line, not a descendant of c2 -- used for force tests.
            self.sidetrack = self._commit(db, repo.id, cid('side'), {'app.py': b'side\n'}, [self.c1])
            BranchCRUD.update_branch_head(db, repo.id, 'main', self.c2)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _commit(db, repo_id, commit_id, files, parents):
        CommitCRUD.create_commit(db, {
            'id': commit_id,
            'repository_id': repo_id,
            'author': 'protect_admin',
            'message': f'commit {commit_id[:6]}',
            'parents': parents,
            'files': {p: base64.b64encode(c).decode() for p, c in files.items()},
            'timestamp': datetime.utcnow().isoformat(),
        })
        return commit_id

    @property
    def auth(self):
        return {'Authorization': f'Bearer {self.admin_token}'}

    def set_mode(self, mode, pattern='main'):
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': pattern, 'mode': mode},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['policy']

    def head_of(self, branch='main'):
        db = self.Session()
        try:
            row = BranchCRUD.get_branch(db, self.repo_id, branch)
            return row.head_commit_id if row else None
        finally:
            db.close()


    def test_changing_mode_does_not_wipe_review_rules(self):
        """A mode button must not silently delete a branch's approval requirement.

        The rules field carries the review requirement now, so treating an omitted
        rules field as "clear them" meant tightening a branch to frozen quietly
        dropped the approvals it demanded.
        """
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': 'protected',
                  'rules': {'required_approvals': 2}},
        )
        self.assertEqual(response.status_code, 200, response.text)

        # Change only the mode, exactly as the mode buttons do.
        self.set_mode('frozen')

        listing = self.client.get(
            f'/api/repository/{self.repo_id}/branch-protection', headers=self.auth
        ).json()
        policy = next(p for p in listing['policies'] if p['branch_pattern'] == 'main')
        self.assertEqual(policy['rules'].get('required_approvals'), 2)
        self.assertEqual(listing['branches']['main']['review']['required_approvals'], 2)

    def test_rules_can_still_be_cleared_explicitly(self):
        """Sending an empty rules object is the deliberate way to clear them."""
        self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': 'protected',
                  'rules': {'required_approvals': 2}},
        )
        self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': 'protected', 'rules': {}},
        )
        listing = self.client.get(
            f'/api/repository/{self.repo_id}/branch-protection', headers=self.auth
        ).json()
        policy = next(p for p in listing['policies'] if p['branch_pattern'] == 'main')
        self.assertEqual(policy['rules'], {})

    # ------------------------------------------------- the mode decision matrix

    def test_mode_matrix_matches_specification(self):
        expectations = {
            protection.MODE_OPEN: dict(update=True, merge=True, force_update=True,
                                       delete=True, rename=True),
            protection.MODE_PROTECTED: dict(update=False, merge=True, force_update=False,
                                            delete=False, rename=False),
            protection.MODE_FROZEN: dict(update=False, merge=False, force_update=False,
                                         delete=False, rename=False),
            protection.MODE_ARCHIVED: dict(update=False, merge=False, force_update=False,
                                           delete=False, rename=False),
        }
        for mode, ops in expectations.items():
            policy = protection.EffectivePolicy(mode=mode)
            for operation, allowed in ops.items():
                self.assertEqual(
                    policy.permits(operation), allowed,
                    f"{mode}.{operation} should be {'allowed' if allowed else 'denied'}",
                )

    def test_most_restrictive_policy_wins(self):
        """A permissive pattern must not loosen a stricter exact-name rule."""
        self.set_mode(protection.MODE_OPEN, pattern='*')
        self.set_mode(protection.MODE_FROZEN, pattern='main')
        db = self.Session()
        try:
            repo = RepositoryCRUD.get_repository(db, self.repo_id)
            effective = protection.resolve_effective_policy(db, repo, 'main')
            self.assertEqual(effective.mode, protection.MODE_FROZEN)
        finally:
            db.close()

    # ------------------------------------------------- operations that must fail

    def test_admin_cannot_direct_push_to_protected_branch(self):
        """Specification 20.3: administration does not bypass protection."""
        self.set_mode(protection.MODE_PROTECTED)
        response = self.client.post(
            f'/api/repository/{self.repo_id}/push',
            headers=self.auth,
            json={'commit': {
                'id': hashlib.sha1(f'{self.id()}:push'.encode()).hexdigest(),
                'repository_id': self.repo_id, 'author': 'protect_admin',
                'message': 'sneak past protection', 'parents': [self.c2],
                'files': {'app.py': base64.b64encode(b'three\n').decode()},
                'timestamp': datetime.utcnow().isoformat(),
            }, 'branch': 'main'},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'DIRECT_PUSH_DENIED')
        self.assertEqual(self.head_of(), self.c2)

    def test_frozen_branch_rejects_move_head(self):
        self.set_mode(protection.MODE_FROZEN)
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c1},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'BRANCH_FROZEN')
        self.assertEqual(self.head_of(), self.c2)

    def test_frozen_branch_rejects_delete(self):
        self.client.post(
            f'/api/repository/{self.repo_id}/branches',
            headers=self.auth, json={'name': 'doomed', 'from_branch': 'main'},
        )
        self.set_mode(protection.MODE_FROZEN, pattern='doomed')
        response = self.client.delete(
            f'/api/repository/{self.repo_id}/branches/doomed', headers=self.auth
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'BRANCH_FROZEN')
        self.assertIsNotNone(self.head_of('doomed'))

    def test_frozen_branch_rejects_rename(self):
        self.client.post(
            f'/api/repository/{self.repo_id}/branches',
            headers=self.auth, json={'name': 'keepname', 'from_branch': 'main'},
        )
        self.set_mode(protection.MODE_FROZEN, pattern='keepname')
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branches/keepname/rename',
            headers=self.auth, json={'new_name': 'escaped'},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'BRANCH_FROZEN')
        self.assertIsNotNone(self.head_of('keepname'))

    def test_frozen_branch_rejects_rollback(self):
        """An alternate mutation path: rollback moved branches without any policy check."""
        self.set_mode(protection.MODE_FROZEN)
        response = self.client.post(
            f'/api/repository/{self.repo_id}/rollback/branch',
            headers=self.auth,
            json={'branch': 'main', 'target_commit_id': self.c1},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'BRANCH_FROZEN')
        self.assertEqual(self.head_of(), self.c2)

    def test_frozen_branch_rejects_cherry_pick(self):
        self.set_mode(protection.MODE_FROZEN)
        response = self.client.post(
            f'/api/repository/{self.repo_id}/cherry-pick',
            headers=self.auth,
            json={'commit_id': self.sidetrack, 'branch': 'main'},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.head_of(), self.c2)

    def test_force_update_disguised_as_a_normal_push_is_refused(self):
        """A non-descendant target is a force update whatever the caller calls it."""
        self.set_mode(protection.MODE_PROTECTED)
        db = self.Session()
        try:
            repo = RepositoryCRUD.get_repository(db, self.repo_id)
            with self.assertRaises(refs.ReferenceError) as caught:
                refs.update_reference(
                    db, repository=repo, actor=self.admin, branch_name='main',
                    new_commit_id=self.sidetrack, operation=refs.OP_UPDATE,
                )
            self.assertEqual(caught.exception.code, 'FORCE_UPDATE_DENIED')
        finally:
            db.close()
        self.assertEqual(self.head_of(), self.c2)

    def test_open_branch_still_accepts_normal_work(self):
        """Protection must not break ordinary development."""
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.head_of(), self.c1)

    # ------------------------------------------------- concurrency

    def test_generation_increments_and_stale_update_is_refused(self):
        db = self.Session()
        try:
            repo = RepositoryCRUD.get_repository(db, self.repo_id)
            before = db.query(Branch).filter(
                Branch.repository_id == self.repo_id, Branch.name == 'main'
            ).first().generation or 0

            refs.update_reference(
                db, repository=repo, actor=self.admin, branch_name='main',
                new_commit_id=self.c1, expected_generation=before,
            )
            after = db.query(Branch).filter(
                Branch.repository_id == self.repo_id, Branch.name == 'main'
            ).first().generation
            self.assertEqual(after, before + 1, "generation must increase monotonically")

            # A second writer still holding the old generation must lose.
            with self.assertRaises(refs.ReferenceError) as caught:
                refs.update_reference(
                    db, repository=repo, actor=self.admin, branch_name='main',
                    new_commit_id=self.c2, expected_generation=before,
                )
            self.assertEqual(caught.exception.code, 'STALE_REFERENCE')
        finally:
            db.close()

    def test_stale_expected_commit_is_refused(self):
        db = self.Session()
        try:
            repo = RepositoryCRUD.get_repository(db, self.repo_id)
            with self.assertRaises(refs.ReferenceError) as caught:
                refs.update_reference(
                    db, repository=repo, actor=self.admin, branch_name='main',
                    new_commit_id=self.c1, expected_commit_id=self.c1,  # actual head is c2
                )
            self.assertEqual(caught.exception.code, 'STALE_REFERENCE')
        finally:
            db.close()

    # ------------------------------------------------- unlock grants

    def _grant_unlock(self, operations=('update',), minutes=30, branch='main'):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/branches/{branch}/unlock-requests',
            headers=self.auth,
            json={'reason': 'documented emergency for the test suite',
                  'operations': list(operations), 'expires_in_minutes': minutes},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['unlock']

    def test_unlock_permits_exactly_one_operation_then_expires(self):
        self.set_mode(protection.MODE_FROZEN)
        # Moving main back to an earlier commit discards history, so the reference
        # service classifies it as a force update -- the grant has to say so.
        self._grant_unlock(operations=('update', 'force_update'))

        first = self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c1},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.head_of(), self.c1)

        # Single use: the grant is spent, so the branch is frozen again.
        second = self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c2},
        )
        self.assertEqual(second.status_code, 403, second.text)
        self.assertEqual(second.json().get('error'), 'BRANCH_FROZEN')
        self.assertEqual(self.head_of(), self.c1)

    def test_expired_unlock_is_not_honoured(self):
        self.set_mode(protection.MODE_FROZEN)
        grant = self._grant_unlock()
        db = self.Session()
        try:
            row = db.query(BranchUnlock).filter(BranchUnlock.id == grant['id']).first()
            row.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        response = self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c1},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.head_of(), self.c2)

    def test_unlock_is_scoped_to_its_operations(self):
        """A grant for `update` must not authorise a delete."""
        self.client.post(
            f'/api/repository/{self.repo_id}/branches',
            headers=self.auth, json={'name': 'scoped', 'from_branch': 'main'},
        )
        self.set_mode(protection.MODE_FROZEN, pattern='scoped')
        self._grant_unlock(operations=('update',), branch='scoped')

        response = self.client.delete(
            f'/api/repository/{self.repo_id}/branches/scoped', headers=self.auth
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNotNone(self.head_of('scoped'))

    def test_unlock_requires_a_written_reason(self):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/branches/main/unlock-requests',
            headers=self.auth, json={'reason': 'oops', 'operations': ['update']},
        )
        self.assertEqual(response.status_code, 400)

    def test_weakening_a_frozen_policy_requires_an_unlock(self):
        self.set_mode(protection.MODE_FROZEN)
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': protection.MODE_OPEN},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get('error'), 'UNLOCK_REQUIRED')

        self._grant_unlock()
        allowed = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': protection.MODE_OPEN},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_unlock_spent_on_a_policy_edit_cannot_also_authorise_a_push(self):
        """A one-time grant must not pay for two things.

        Found end-to-end: relaxing a frozen policy consumed nothing, so the same grant
        was still live afterwards and let the next push through a branch that was by
        then merely protected.
        """
        self.set_mode(protection.MODE_FROZEN)
        self._grant_unlock(operations=('update',))

        relaxed = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': protection.MODE_PROTECTED},
        )
        self.assertEqual(relaxed.status_code, 200, relaxed.text)

        # The grant paid for the policy change; it must not also pay for a direct push.
        pushed = self.client.post(
            f'/api/repository/{self.repo_id}/push',
            headers=self.auth,
            json={'commit': {
                'id': hashlib.sha1(f'{self.id()}:reuse'.encode()).hexdigest(),
                'repository_id': self.repo_id, 'author': 'protect_admin',
                'message': 'should not land', 'parents': [self.c2],
                'files': {'app.py': base64.b64encode(b'nope\n').decode()},
                'timestamp': datetime.utcnow().isoformat(),
            }, 'branch': 'main'},
        )
        self.assertEqual(pushed.status_code, 403, pushed.text)
        self.assertEqual(pushed.json().get('error'), 'DIRECT_PUSH_DENIED')
        self.assertEqual(self.head_of(), self.c2)

    def test_policy_edit_uses_optimistic_concurrency(self):
        policy = self.set_mode(protection.MODE_PROTECTED)
        response = self.client.put(
            f'/api/repository/{self.repo_id}/branch-protection',
            headers=self.auth,
            json={'branch_pattern': 'main', 'mode': protection.MODE_FROZEN,
                  'expected_policy_version': policy['policy_version'] + 5},
        )
        self.assertEqual(response.status_code, 409, response.text)

    # ------------------------------------------------- immutable releases

    def test_release_name_can_never_be_reused(self):
        first = self.client.post(
            f'/api/repository/{self.repo_id}/releases/immutable',
            headers=self.auth, json={'name': 'v1.0.0', 'commit_id': self.c1},
        )
        self.assertEqual(first.status_code, 200, first.text)

        second = self.client.post(
            f'/api/repository/{self.repo_id}/releases/immutable',
            headers=self.auth, json={'name': 'v1.0.0', 'commit_id': self.c2},
        )
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json().get('error'), 'IMMUTABLE_REFERENCE')

        listing = self.client.get(
            f'/api/repository/{self.repo_id}/releases/immutable', headers=self.auth
        ).json()['releases']
        held = [r for r in listing if r['name'] == 'v1.0.0']
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]['commit_id'], self.c1,
                         "the release must still point at the commit it was created with")

    def test_release_has_a_signed_manifest(self):
        response = self.client.post(
            f'/api/repository/{self.repo_id}/releases/immutable',
            headers=self.auth, json={'name': 'v2.0.0', 'commit_id': self.c2},
        )
        body = response.json()['release']
        self.assertTrue(body['signature'])
        self.assertEqual(body['manifest']['commit'], self.c2)

    def test_no_route_can_move_or_delete_a_release(self):
        """The guarantee is the absence of a mutation path; assert it stays absent."""
        paths = set(server_module.app.openapi()['paths'])
        for path, methods in server_module.app.openapi()['paths'].items():
            if 'releases/immutable' in path:
                self.assertFalse(
                    {'put', 'patch', 'delete'} & set(methods),
                    f"{path} must not expose a way to change a release",
                )

    # ------------------------------------------------- audit

    def test_denied_operations_are_audited(self):
        self.set_mode(protection.MODE_FROZEN)
        self.client.put(
            f'/api/repository/{self.repo_id}/branches/main/head',
            headers=self.auth, json={'head_commit_id': self.c1},
        )
        response = self.client.get(
            f'/api/repository/{self.repo_id}/audit/references',
            headers=self.auth, params={'reference': 'main'},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        denials = [e for e in body['events'] if e['decision'] == 'denied']
        self.assertTrue(denials, "a refused update must leave an audit record")
        self.assertEqual(denials[0]['error_code'], 'BRANCH_FROZEN')
        self.assertTrue(body['chain_intact'])

    def test_audit_chain_detects_tampering(self):
        # Several events, so there is a chain to break rather than a single link.
        for target in (self.c1, self.c2, self.c1):
            self.client.put(
                f'/api/repository/{self.repo_id}/branches/main/head',
                headers=self.auth, json={'head_commit_id': target},
            )
        db = self.Session()
        try:
            events = (
                db.query(RefAuditEvent)
                .filter(RefAuditEvent.repository_id == self.repo_id)
                .order_by(RefAuditEvent.id)
                .all()
            )
            self.assertGreaterEqual(len(events), 2, "expected a chain of events")
            # Rewrite an event in the middle, as someone covering their tracks would.
            events[0].event_hash = 'tampered' + '0' * 56
            db.commit()
        finally:
            db.close()

        body = self.client.get(
            f'/api/repository/{self.repo_id}/audit/references', headers=self.auth
        ).json()
        self.assertFalse(body['chain_intact'],
                         "editing an event must break the hash chain")


if __name__ == '__main__':
    unittest.main()
