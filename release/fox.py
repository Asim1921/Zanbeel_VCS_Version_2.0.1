#!/usr/bin/env python3
"""
Fox Client - Local version control client for FoxNest
Optimized with Git-like compression, delta encoding, and pack files
"""

import os
import json
import hashlib
import shutil
import argparse
import base64
import sys
import zlib
import difflib
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from pathlib import Path
import requests
import urllib.parse
import re

# ---------------------------------------------------------------------------
# .foxignore support
#
# Gitignore syntax: globs, ! to re-include, leading / to anchor to the repository
# root, trailing / for directories, ** to span directories. Rules are evaluated in
# order and the last match wins.
#
# This mirrors server/app/services/ignore_rules.py. The server enforces the same
# rules on push, so a stale client cannot smuggle build output into the store --
# keep the two implementations in step.
# ---------------------------------------------------------------------------

IGNORE_FILENAME = ".foxignore"

DEFAULT_IGNORE_PATTERNS = (
    ".fox/", ".git/", ".svn/", ".hg/",
    "node_modules/", "venv/", ".venv/", "env/", "vendor/",
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".tox/", ".mypy_cache/",
    "build/", "dist/", "out/", "target/", "bin/", "obj/",
    "*.o", "*.so", "*.dylib", "*.class",
    "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.rar", "*.7z",
    "*.exe", "*.msi", "*.dmg", "*.apk", "*.iso", "*.bacpac",
    ".DS_Store", "Thumbs.db", "*.log", "*.tmp", "*.swp",
)

DEFAULT_FOXIGNORE_TEMPLATE = """\
# .foxignore -- paths Zanbeel must never store.
# Same syntax as .gitignore: globs, ! to re-include, / to anchor, trailing / for directories.
# This file replaces the built-in defaults, so keep the entries you still want.

# Version control and tooling metadata
.fox/
.git/

# Dependencies and virtual environments
node_modules/
venv/
.venv/
__pycache__/
*.py[cod]

# Build output
build/
dist/
out/
target/
obj/

# Archives and installers -- these are what bloat a repository
*.zip
*.tar.gz
*.7z
*.exe
*.msi
*.apk
*.iso

# Local noise
.DS_Store
Thumbs.db
*.log
"""


def _ignore_translate(pattern):
    """Translate one gitignore glob into a regular expression source string."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                if pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = i + 1
            if end < n and pattern[end] in ("!", "^"):
                end += 1
            if end < n and pattern[end] == "]":
                end += 1
            while end < n and pattern[end] != "]":
                end += 1
            if end >= n:
                out.append(re.escape(ch))
                i += 1
            else:
                body = pattern[i + 1:end]
                if body and body[0] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _compile_ignore_rule(raw):
    line = raw.rstrip("\n").rstrip("\r")
    if not line.endswith("\\"):
        line = line.rstrip()
    if not line or line.lstrip().startswith("#"):
        return None

    negated = line.startswith("!")
    if negated:
        line = line[1:]
    if line.startswith("\\") and len(line) > 1 and line[1] in ("#", "!"):
        line = line[1:]
    if not line:
        return None
    if line.endswith("/"):
        line = line[:-1]
    if not line:
        return None

    anchored = line.startswith("/") or "/" in line.rstrip("/")
    line = line.lstrip("/")
    if not line:
        return None

    prefix = "" if anchored else "(?:.*/)?"
    return (re.compile("^" + prefix + _ignore_translate(line) + "(?:/.*)?$"), negated, raw.strip())


class IgnoreMatcher:
    """Ordered .foxignore rules; the last matching rule wins."""

    def __init__(self, rules):
        self._rules = rules

    @classmethod
    def from_text(cls, text):
        rules = []
        for raw in (text or "").splitlines():
            rule = _compile_ignore_rule(raw)
            if rule is not None:
                rules.append(rule)
        return cls(rules)

    @classmethod
    def default(cls):
        return cls.from_text("\n".join(DEFAULT_IGNORE_PATTERNS))

    def __bool__(self):
        return bool(self._rules)

    def matched_rule(self, path):
        normalized = (path or "").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        if not normalized:
            return None

        decision = None
        for regex, negated, source in self._rules:
            if regex.match(normalized):
                decision = (negated, source)
        if decision is None or decision[0]:
            return None
        return decision[1]

    def is_ignored(self, path):
        return self.matched_rule(path) is not None


def load_ignore_matcher(root="."):
    """Repository .foxignore when present, otherwise the built-in defaults."""
    try:
        candidate = Path(root) / IGNORE_FILENAME
        if candidate.is_file():
            matcher = IgnoreMatcher.from_text(candidate.read_text(encoding="utf-8", errors="replace"))
            if matcher:
                return matcher
    except Exception:
        pass
    return IgnoreMatcher.default()


def find_repository_root(start=None):
    """Walk upwards looking for a .fox repository, the way git finds .git.

    Returns (root, prefix) where prefix is the starting directory expressed
    relative to root, or (None, "") when there is no repository above us.

    Without this, `.fox` was only ever looked for in the current directory, so
    `cd src && fox status` reported "src not a repository" even from inside a
    perfectly good checkout. That bites hardest in an editor, where the
    integrated terminal often opens in a subfolder.
    """
    current = Path(start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".fox" / "config.json").is_file():
            try:
                prefix = current.relative_to(candidate).as_posix()
            except ValueError:
                prefix = ""
            return candidate, ("" if prefix == "." else prefix)
    return None, ""


class FoxClient:
    def __init__(self, discover=True):
        # Commands run from the repository root so that every path below stays
        # relative and behaves exactly as it always has. `path_prefix` records
        # where the user actually was, so arguments they typed can be mapped
        # back onto the root (see `resolve_user_path`).
        self.path_prefix = ""
        self.repo_root = None
        if discover:
            root, prefix = find_repository_root()
            if root is not None and Path.cwd().resolve() != root:
                self.repo_root = root
                self.path_prefix = prefix
                os.chdir(root)
            elif root is not None:
                self.repo_root = root

        self.fox_dir = Path(".fox")
        self.config_file = self.fox_dir / "config.json"
        self.staging_dir = self.fox_dir / "staging"
        self.objects_dir = self.fox_dir / "objects"
        self.packs_dir = self.fox_dir / "packs"  # Git-like pack files
        self.commits_file = self.fox_dir / "commits.json"
        self.head_file = self.fox_dir / "HEAD"
        self.index_file = self.fox_dir / "index.json"  # Git-like index for fast tracking
        self.delta_cache_file = self.fox_dir / "delta_cache.json"  # Cache for delta relationships
        self.refs_dir = self.fox_dir / "refs"
        self.heads_dir = self.refs_dir / "heads"
        self.tags_dir = self.refs_dir / "tags"
        self.merge_conflicts_file = self.fox_dir / "merge_conflicts.json"
        
        # Global credentials file (in user's home directory)
        self.global_config_dir = Path.home() / ".foxnest"
        self.global_config_file = self.global_config_dir / "credentials.json"
        
        # Compression settings
        self.compression_level = 6  # zlib compression level (1-9)
        self.pack_threshold = 20  # Number of objects before creating pack
        
        # Default server configuration
        self.server_url = "http://192.168.0.11:33333"
        
        # Load server URL from config if available
        if self.is_initialized():
            config = self.load_config()
            if config and 'server_url' in config:
                self.server_url = config['server_url']
            self.ensure_refs()
    
    def load_global_config(self):
        """Load global FoxNest credentials"""
        if not self.global_config_file.exists():
            return {}
        
        try:
            with open(self.global_config_file, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load global config: {e}")
            return {}

    @staticmethod
    def compute_repo_id(username, repo_name):
        """Match server RepositoryCRUD.generate_repo_id (requester-owned repos)."""
        return hashlib.md5(f"{username}_{repo_name}".encode()).hexdigest()[:16]

    def resolve_remote_repo_id(self, config):
        """Resolve server repo_id for this local working copy."""
        if config.get("repo_id"):
            return config["repo_id"]

        self._sync_server_url(config)
        server_url = self._resolve_server_url()

        if not self.get_auth_headers():
            print("Tip: run 'fox login' so the client can discover your remote repository.")

        repo_id = self.get_existing_repository_id(config)
        if repo_id:
            return repo_id

        username = config.get("username")
        for name in self._repo_name_variants(config.get("repo_name")):
            expected = self.compute_repo_id(username, name)
            try:
                resp = self._authed_get(
                    f"{server_url}/api/repository/{expected}",
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        return expected
            except Exception:
                pass

        return None

    def link_remote_repository(self, config):
        """Persist repo_id in local config when the remote repo already exists."""
        repo_id = self.resolve_remote_repo_id(config)
        if not repo_id:
            return None
        if config.get("repo_id") != repo_id:
            config["repo_id"] = repo_id
            self.save_config(config)
            print(f"Linked to remote repository: {repo_id}")
        return repo_id

    def has_auth_token(self):
        """True if global credentials contain a non-empty access token."""
        return bool(self.get_auth_headers())

    def ensure_authenticated(self, interactive=True):
        """Ensure an access token is available; optionally prompt for login."""
        if self.has_auth_token():
            return True
        if not interactive:
            return False
        print(f"No access token in {self.global_config_file}")
        print("Run: fox login")
        return self.login()

    @staticmethod
    def _response_error_message(response):
        """Extract a human-readable error from a requests response."""
        try:
            content_type = (response.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                data = response.json()
                detail = data.get("detail")
                if isinstance(detail, list):
                    parts = []
                    for item in detail:
                        if isinstance(item, dict):
                            loc = ".".join(str(x) for x in item.get("loc", []))
                            parts.append(f"{loc}: {item.get('msg', item)}")
                        else:
                            parts.append(str(item))
                    detail = "; ".join(parts) if parts else None
                return detail or data.get("error") or data.get("message") or "Unknown error"
        except Exception:
            pass
        return (response.text or "").strip() or f"HTTP {response.status_code}"

    def get_auth_headers(self):
        """Return Authorization headers for the current credential.

        FOXNEST_TOKEN wins over the credentials file. That is what lets a host
        process -- the VS Code extension, or a CI runner -- hand the token in
        from an OS keychain or a secret store, so it never has to be written to
        ~/.foxnest/credentials.json in the clear. When it is unset, behaviour is
        exactly as it was.
        """
        env_token = (os.environ.get("FOXNEST_TOKEN") or "").strip()
        if env_token:
            return {"Authorization": f"Bearer {env_token}"}

        cfg = self.load_global_config() or {}
        # support multiple possible token key names
        token = cfg.get('access_token') or cfg.get('token') or cfg.get('auth_token') or cfg.get('accessToken')
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _resolve_actor_username(self, preferred=None):
        """Resolve best-effort actor username for permission-sensitive server calls."""
        candidates = []
        if preferred:
            candidates.append(preferred)

        global_cfg = self.load_global_config() or {}
        local_cfg = self.load_config() or {}

        candidates.extend([
            global_cfg.get('username'),
            global_cfg.get('user'),
            local_cfg.get('username'),
            local_cfg.get('user')
        ])

        try:
            if self.commits_file.exists():
                with open(self.commits_file, "r") as f:
                    commits = json.load(f)
                if commits:
                    last_author = commits[-1].get('author')
                    if last_author:
                        candidates.append(last_author)
        except Exception:
            pass

        for value in candidates:
            if value and str(value).strip():
                return str(value).strip()
        return None

    def login(self, username=None, password=None):
        """Prompt for username/password, call server login, and save token to global config."""
        server_url = self._resolve_server_url()
        global_cfg = self.load_global_config() or {}

        if not username:
            default_username = global_cfg.get("username")
            if default_username:
                entered = input(f"Username [{default_username}]: ").strip()
                username = entered or default_username
            else:
                username = input("Username: ")
        if not password:
            # Use getpass to avoid echoing password
            try:
                import getpass
                password = getpass.getpass("Password: ")
            except Exception:
                password = input("Password: ")

        print(f"Logging in to: {server_url}")
        try:
            response = requests.post(
                f"{server_url}/api/auth/login",
                json={"username": username, "password": password},
                timeout=30
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error during login: {e}")
            return False

        if response.status_code == 200:
            try:
                data = response.json()
                if data.get('success') and data.get('access_token'):
                    token = data['access_token']
                    cfg = self.load_global_config() or {}
                    cfg['username'] = username
                    cfg['access_token'] = token
                    cfg['origin_url'] = server_url
                    if self.save_global_config(cfg):
                        print(f"Login successful. Token saved to: {self.global_config_file}")
                        return True
                    else:
                        print("Login succeeded but failed to save token to global config")
                        return False
                else:
                    print(f"Login failed: {data.get('message') or data.get('error') or 'Unknown error'}")
                    return False
            except Exception as e:
                print(f"Failed to parse login response: {e}")
                return False
        elif response.status_code in (401, 403):
            try:
                data = response.json()
                print(f"Authentication failed: {data.get('detail') or data.get('error') or response.text}")
            except Exception:
                print(f"Authentication failed (HTTP {response.status_code})")
            return False
        else:
            print(f"HTTP error during login: {response.status_code}")
            return False

    def logout(self):
        """Remove access token from global config"""
        cfg = self.load_global_config() or {}
        removed = False
        for key in ('access_token', 'token', 'auth_token', 'accessToken'):
            if key in cfg:
                del cfg[key]
                removed = True
        if self.save_global_config(cfg):
            if removed:
                print(f"Logged out. Token removed from: {self.global_config_file}")
            else:
                print("No token found in global config")
            return True
        print("Failed to update global config")
        return False

    # ------------------------------------------------------------------
    # Personal access tokens
    #
    # `fox login` still exists, but it stores a session credential derived from
    # the account password. A token is the better thing to put on a build agent:
    # it is named, scoped, and can be revoked on its own without disturbing any
    # other machine or forcing a password change.
    # ------------------------------------------------------------------

    def token_create(self, name, scopes=None, expires_in_days=None, use=False):
        """Create a personal access token on the server and print it once."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False

        payload = {"name": name}
        if scopes:
            payload["scopes"] = scopes
        if expires_in_days:
            payload["expires_in_days"] = int(expires_in_days)

        try:
            response = requests.post(
                f"{server_url}/api/tokens",
                json=payload,
                headers=self.get_auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to create token: {self._response_error_message(response)}")
            return False

        data = response.json()
        token = data.get("token", "")
        meta = data.get("access_token", {})

        print("")
        print("Access token created.")
        print("")
        print(f"  {token}")
        print("")
        print("  Copy it now - the server stores only a hash and cannot show it again.")
        print(f"  name    : {meta.get('name')}")
        print(f"  scopes  : {', '.join(meta.get('scopes') or [])}")
        print(f"  expires : {meta.get('expires_at') or 'never'}")
        print("")

        if use:
            cfg = self.load_global_config() or {}
            cfg["access_token"] = token
            cfg["origin_url"] = server_url
            if self.save_global_config(cfg):
                print(f"Saved as the active credential in {self.global_config_file}")
            else:
                print("Warning: token created but could not be saved to global config")
        else:
            print("To use it on this machine:  fox token use <token>")
        return True

    def token_list(self, include_revoked=False):
        """List the tokens belonging to the logged-in user."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False

        try:
            response = requests.get(
                f"{server_url}/api/tokens",
                params={"include_revoked": str(bool(include_revoked)).lower()},
                headers=self.get_auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to list tokens: {self._response_error_message(response)}")
            return False

        tokens = response.json().get("tokens", [])
        if not tokens:
            print("No access tokens. Create one with: fox token create <name>")
            return True

        print(f"{'ID':<5} {'NAME':<22} {'PREFIX':<12} {'SCOPES':<26} {'LAST USED':<20} STATE")
        for t in tokens:
            state = "revoked" if t.get("revoked") else ("expired" if t.get("expired") else "active")
            last_used = (t.get("last_used_at") or "never")[:19]
            print(
                f"{t['id']:<5} {t['name'][:21]:<22} {t['prefix']:<12} "
                f"{','.join(t.get('scopes') or [])[:25]:<26} {last_used:<20} {state}"
            )
        return True

    def token_revoke(self, token_id):
        """Revoke one token by id."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False

        try:
            response = requests.delete(
                f"{server_url}/api/tokens/{token_id}",
                headers=self.get_auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code == 404:
            print(f"No token with id {token_id} belongs to you.")
            return False
        if response.status_code != 200:
            print(f"Failed to revoke token: {self._response_error_message(response)}")
            return False

        print(f"Token {token_id} revoked. Any machine using it is now locked out.")
        return True

    def token_use(self, token):
        """Store an existing token as this machine's credential.

        Verified against the server before saving, so a mistyped token fails
        here rather than on the next push.
        """
        server_url = self._resolve_server_url()
        token = (token or "").strip()
        if not token:
            print("A token value is required.")
            return False

        try:
            response = requests.get(
                f"{server_url}/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"That token was rejected by {server_url}: {self._response_error_message(response)}")
            return False

        user = response.json().get("user", {})
        cfg = self.load_global_config() or {}
        cfg["access_token"] = token
        cfg["origin_url"] = server_url
        if user.get("username"):
            cfg["username"] = user["username"]
        if not self.save_global_config(cfg):
            print("Failed to save token to global config")
            return False

        print(f"Authenticated as {user.get('username')} using an access token.")
        print(f"Saved to: {self.global_config_file}")
        return True

    # ------------------------------------------------------------------
    # Webhooks
    #
    # Repository-scoped by default (the repo this working copy is linked to);
    # --global manages the server-wide hooks that fire for every repository.
    # ------------------------------------------------------------------

    def _webhook_scope(self, use_global):
        """Resolve which repository these hook commands apply to, or None."""
        if use_global:
            return None
        repo_id = (self.load_config() or {}).get("repo_id")
        if not repo_id:
            print("This directory is not linked to a server repository.")
            print("Run 'fox set repo-id <id>', or use --global for server-wide hooks.")
            return False
        return repo_id

    def webhook_list(self, use_global=False):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id = self._webhook_scope(use_global)
        if repo_id is False:
            return False

        params = {"repository_id": repo_id} if repo_id else {}
        try:
            response = requests.get(
                f"{server_url}/api/webhooks", params=params,
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to list webhooks: {self._response_error_message(response)}")
            return False

        hooks = response.json().get("webhooks", [])
        if not hooks:
            scope = "server-wide" if repo_id is None else f"repository {repo_id}"
            print(f"No webhooks configured ({scope}).")
            print("Add one with: fox webhook add <url>")
            return True

        print(f"{'ID':<5} {'ACTIVE':<8} {'SIGNED':<8} {'EVENTS':<28} URL")
        for h in hooks:
            print(
                f"{h['id']:<5} {('yes' if h['active'] else 'no'):<8} "
                f"{('yes' if h['has_secret'] else 'no'):<8} "
                f"{','.join(h['events'])[:27]:<28} {h['url']}"
            )
        return True

    def webhook_add(self, url, events=None, secret=None, use_global=False):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id = self._webhook_scope(use_global)
        if repo_id is False:
            return False

        payload = {"url": url, "repository_id": repo_id}
        if events:
            payload["events"] = events
        if secret:
            payload["secret"] = secret

        try:
            response = requests.post(
                f"{server_url}/api/webhooks", json=payload,
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to add webhook: {self._response_error_message(response)}")
            return False

        hook = response.json().get("webhook", {})
        print(f"Webhook {hook.get('id')} created for {hook.get('url')}")
        print(f"  events: {', '.join(hook.get('events') or [])}")
        if not hook.get("has_secret"):
            print("  note: no secret set, so deliveries are unsigned and a receiver")
            print("        cannot tell a real delivery from a forged one.")
        return True

    def webhook_remove(self, webhook_id):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.delete(
                f"{server_url}/api/webhooks/{webhook_id}",
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to remove webhook: {self._response_error_message(response)}")
            return False
        print(f"Webhook {webhook_id} removed.")
        return True

    def webhook_ping(self, webhook_id):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        print(f"Pinging webhook {webhook_id}...")
        try:
            response = requests.post(
                f"{server_url}/api/webhooks/{webhook_id}/ping",
                headers=self.get_auth_headers(), timeout=60,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Ping failed: {self._response_error_message(response)}")
            return False

        delivery = response.json().get("delivery") or {}
        if delivery.get("success"):
            print(f"  OK  HTTP {delivery.get('status_code')} in {delivery.get('duration_ms')}ms")
        else:
            print(f"  FAILED  {delivery.get('error') or 'HTTP ' + str(delivery.get('status_code'))}")
        return bool(delivery.get("success"))

    def webhook_deliveries(self, webhook_id, limit=15):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.get(
                f"{server_url}/api/webhooks/{webhook_id}/deliveries",
                params={"limit": limit},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to fetch deliveries: {self._response_error_message(response)}")
            return False

        deliveries = response.json().get("deliveries", [])
        if not deliveries:
            print("No deliveries recorded yet.")
            return True

        print(f"{'WHEN':<21} {'EVENT':<22} {'TRY':<5} {'CODE':<6} RESULT")
        for d in deliveries:
            when = (d.get("created_at") or "")[:19]
            code = d.get("status_code") or "-"
            result = "ok" if d.get("success") else (d.get("error") or "failed")[:40]
            print(f"{when:<21} {d['event'][:21]:<22} {d.get('attempt', 1):<5} {str(code):<6} {result}")
        return True

    # ------------------------------------------------------------------
    # Status checks
    #
    # This is the surface a CI job uses: report pending when the build starts,
    # then success or failure when it finishes. Authenticate the runner with a
    # repo:write access token rather than someone's password.
    # ------------------------------------------------------------------

    def _status_repo_and_commit(self, commit=None):
        """Resolve which repository and commit a status applies to."""
        config = self.load_config() or {}
        repo_id = config.get("repo_id")
        if not repo_id:
            print("This directory is not linked to a server repository.")
            print("Run: fox set repo-id <id>")
            return None, None

        commit_id = commit or self.get_head_commit_id()
        if not commit_id:
            print("No commit to report against. Pass one explicitly: --commit <id>")
            return None, None
        return repo_id, commit_id

    def status_report(self, context, state, description=None, target_url=None, commit=None):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id, commit_id = self._status_repo_and_commit(commit)
        if not repo_id:
            return False

        payload = {"context": context, "state": state}
        if description:
            payload["description"] = description
        if target_url:
            payload["target_url"] = target_url

        try:
            response = requests.post(
                f"{server_url}/api/repository/{repo_id}/commits/{commit_id}/statuses",
                json=payload, headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to report status: {self._response_error_message(response)}")
            return False

        data = response.json()
        combined = data.get("combined", {})
        print(f"Reported {context} = {state} on {commit_id[:8]}")
        print(f"  commit is now: {combined.get('state')}")
        missing = combined.get("missing_required") or []
        failing = combined.get("failing_required") or []
        if failing:
            print(f"  failing required: {', '.join(failing)}")
        if missing:
            print(f"  not yet reported: {', '.join(missing)}")
        return True

    def status_show(self, commit=None, history=False):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id, commit_id = self._status_repo_and_commit(commit)
        if not repo_id:
            return False

        try:
            response = requests.get(
                f"{server_url}/api/repository/{repo_id}/commits/{commit_id}/statuses",
                params={"history": str(bool(history)).lower()},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to fetch statuses: {self._response_error_message(response)}")
            return False

        data = response.json()

        if history:
            rows = data.get("history", [])
            if not rows:
                print(f"No status reports for {commit_id[:8]}.")
                return True
            print(f"{'WHEN':<21} {'CONTEXT':<26} {'STATE':<10} BY")
            for r in rows:
                print(
                    f"{(r.get('created_at') or '')[:19]:<21} {r['context'][:25]:<26} "
                    f"{r['state']:<10} {r.get('reported_by') or '-'}"
                )
            return True

        combined = data.get("combined", {})
        statuses = combined.get("statuses", [])
        print(f"Commit {commit_id[:8]} - overall: {combined.get('state')}")
        if not statuses:
            print("  no checks have reported")
        else:
            for s in statuses:
                mark = {"success": "ok  ", "failure": "FAIL", "error": "ERR ", "pending": "... "}
                print(f"  [{mark.get(s['state'], '?   ')}] {s['context']}"
                      f"{('  - ' + s['description']) if s.get('description') else ''}")

        required = combined.get("required") or []
        if required:
            print(f"  required: {', '.join(required)}")
            print(f"  merge gate: {'satisfied' if combined.get('required_satisfied') else 'BLOCKED'}")
        return True

    # ------------------------------------------------------------------
    # Server-side push rules (pre-receive hooks)
    # ------------------------------------------------------------------

    def hook_list(self, use_global=False):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id = self._webhook_scope(use_global)
        if repo_id is False:
            return False

        params = {"repository_id": repo_id} if repo_id else {}
        try:
            response = requests.get(
                f"{server_url}/api/server-hooks", params=params,
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to list push rules: {self._response_error_message(response)}")
            return False

        hooks = response.json().get("hooks", [])
        if not hooks:
            print("No push rules configured.")
            print("Add one with: fox hook add <name> --min-message-length 20")
            return True

        for h in hooks:
            state = "enforced" if h["enabled"] else "disabled"
            print(f"[{h['id']}] {h['name']}  ({h['hook_type']}, {state})")
            config = h.get("config") or {}
            if not config:
                print("      no rules - accepts everything")
            for key, value in sorted(config.items()):
                shown = ", ".join(map(str, value)) if isinstance(value, list) else value
                print(f"      {key}: {shown}")
        return True

    def hook_add(self, name, use_global=False, min_message_length=None,
                 message_pattern=None, forbidden_paths=None, protected_paths=None,
                 protected_allowed_users=None, max_file_bytes=None,
                 forbidden_content=None, external_url=None):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        repo_id = self._webhook_scope(use_global)
        if repo_id is False:
            return False

        config = {}
        if min_message_length is not None:
            config["commit_message_min_length"] = int(min_message_length)
        if message_pattern:
            config["commit_message_pattern"] = message_pattern
        if forbidden_paths:
            config["forbidden_paths"] = forbidden_paths
        if protected_paths:
            config["protected_paths"] = protected_paths
        if protected_allowed_users:
            config["protected_paths_allowed_users"] = protected_allowed_users
        if max_file_bytes is not None:
            config["max_file_bytes"] = int(max_file_bytes)
        if forbidden_content:
            config["forbidden_content"] = forbidden_content
        if external_url:
            config["external_url"] = external_url

        if not config:
            print("A rule with no conditions would accept everything. Pass at least one option.")
            return False

        try:
            response = requests.post(
                f"{server_url}/api/server-hooks",
                json={"name": name, "repository_id": repo_id,
                      "hook_type": "pre-receive", "config": config},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to add push rule: {self._response_error_message(response)}")
            return False

        hook = response.json().get("hook", {})
        print(f"Push rule {hook.get('id')} '{hook.get('name')}' created and enforced.")
        print("Check it before it starts refusing colleagues' work:")
        print(f"  fox hook test {hook.get('id')} --message '<a message>' --path <a/path>")
        return True

    def hook_remove(self, hook_id):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.delete(
                f"{server_url}/api/server-hooks/{hook_id}",
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to remove push rule: {self._response_error_message(response)}")
            return False
        print(f"Push rule {hook_id} removed.")
        return True

    def hook_enable(self, hook_id, enabled=True):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.put(
                f"{server_url}/api/server-hooks/{hook_id}",
                json={"enabled": bool(enabled)},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to update push rule: {self._response_error_message(response)}")
            return False
        print(f"Push rule {hook_id} is now {'enforced' if enabled else 'disabled'}.")
        return True

    def hook_test(self, hook_id, message="", paths=None):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.post(
                f"{server_url}/api/server-hooks/{hook_id}/test",
                json={"message": message, "paths": paths or []},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Dry run failed: {self._response_error_message(response)}")
            return False

        data = response.json()
        if data.get("would_accept"):
            print("Would be ACCEPTED.")
        else:
            print("Would be REJECTED:")
            for reason in data.get("reasons", []):
                print(f"  - {reason}")
        print(f"Note: {data.get('note')}")
        return bool(data.get("would_accept"))

    # ------------------------------------------------------------------
    # SSH key authentication
    #
    # `fox ssh login` signs a one-time nonce with a local private key and trades
    # it for a session token. Nothing reusable crosses the wire, so unlike a
    # password or a stored token there is no long-lived secret to capture.
    # ------------------------------------------------------------------

    DEFAULT_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")

    def _find_private_key(self, explicit=None):
        """Locate a private key: the one given, else the usual names in ~/.ssh."""
        from pathlib import Path

        if explicit:
            path = Path(explicit).expanduser()
            if not path.exists():
                print(f"No such key file: {path}")
                return None
            return path

        ssh_dir = Path.home() / ".ssh"
        for name in self.DEFAULT_KEY_NAMES:
            candidate = ssh_dir / name
            if candidate.exists():
                return candidate

        print(f"No private key found in {ssh_dir}.")
        print("Generate one with:  ssh-keygen -t ed25519")
        print("Or point at it with: fox ssh login --key <path>")
        return None

    def ssh_key_add(self, title, key_path=None):
        """Upload a public key to the server."""
        from pathlib import Path

        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False

        if key_path:
            pub_path = Path(key_path).expanduser()
        else:
            private = self._find_private_key()
            if not private:
                return False
            pub_path = private.with_suffix(private.suffix + ".pub") if private.suffix else Path(str(private) + ".pub")

        if not pub_path.exists():
            print(f"No public key at {pub_path}")
            return False

        try:
            public_key = pub_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"Could not read {pub_path}: {e}")
            return False

        try:
            response = requests.post(
                f"{server_url}/api/ssh-keys",
                json={"title": title, "public_key": public_key},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to add key: {self._response_error_message(response)}")
            return False

        key = response.json().get("key", {})
        print(f"Added {key.get('key_type')} key '{key.get('title')}'")
        print(f"  fingerprint: {key.get('fingerprint')}")
        print("Sign in with it using: fox ssh login")
        return True

    def ssh_key_list(self):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.get(
                f"{server_url}/api/ssh-keys", headers=self.get_auth_headers(), timeout=30
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"Failed to list keys: {self._response_error_message(response)}")
            return False

        keys = response.json().get("keys", [])
        if not keys:
            print("No SSH keys registered. Add one with: fox ssh add <title>")
            return True

        print(f"{'ID':<5} {'TITLE':<22} {'TYPE':<16} {'LAST USED':<20} FINGERPRINT")
        for k in keys:
            print(
                f"{k['id']:<5} {k['title'][:21]:<22} {k['key_type']:<16} "
                f"{(k.get('last_used_at') or 'never')[:19]:<20} {k['fingerprint']}"
            )
        return True

    def ssh_key_remove(self, key_id):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.delete(
                f"{server_url}/api/ssh-keys/{key_id}",
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to remove key: {self._response_error_message(response)}")
            return False
        print(f"Key {key_id} removed. Any machine relying on it can no longer sign in.")
        return True

    def ssh_login(self, username=None, key_path=None):
        """Authenticate with a private key and save the resulting session token."""
        server_url = self._resolve_server_url()
        global_cfg = self.load_global_config() or {}

        if not username:
            username = global_cfg.get("username")
        if not username:
            username = input("Username: ").strip()
        if not username:
            print("A username is required.")
            return False

        private_path = self._find_private_key(key_path)
        if not private_path:
            return False

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
        except ImportError:
            print("SSH login needs the 'cryptography' package on the client:")
            print("  pip install cryptography")
            return False

        try:
            key_bytes = private_path.read_bytes()
        except Exception as e:
            print(f"Could not read {private_path}: {e}")
            return False

        try:
            private_key = serialization.load_ssh_private_key(key_bytes, password=None)
        except TypeError:
            # Passphrase-protected keys need the passphrase; ask rather than fail.
            try:
                import getpass
                passphrase = getpass.getpass(f"Passphrase for {private_path.name}: ").encode()
                private_key = serialization.load_ssh_private_key(key_bytes, password=passphrase)
            except Exception as e:
                print(f"Could not unlock the key: {e}")
                return False
        except Exception as e:
            print(f"Could not read the private key: {e}")
            return False

        try:
            response = requests.post(
                f"{server_url}/api/auth/ssh/challenge",
                json={"username": username}, timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Could not get a challenge: {self._response_error_message(response)}")
            return False

        nonce = response.json().get("nonce", "")
        message = nonce.encode("utf-8")

        try:
            if isinstance(private_key, ed25519.Ed25519PrivateKey):
                signature = private_key.sign(message)
            elif isinstance(private_key, rsa.RSAPrivateKey):
                signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            elif isinstance(private_key, ec.EllipticCurvePrivateKey):
                signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
            else:
                print(f"Unsupported key type: {type(private_key).__name__}")
                return False
        except Exception as e:
            print(f"Could not sign the challenge: {e}")
            return False

        import base64 as _b64
        try:
            response = requests.post(
                f"{server_url}/api/auth/ssh/verify",
                json={"nonce": nonce, "signature": _b64.b64encode(signature).decode()},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False

        if response.status_code != 200:
            print(f"SSH login failed: {self._response_error_message(response)}")
            print(f"Is {private_path.name}'s public key registered? Check: fox ssh list")
            return False

        data = response.json()
        cfg = self.load_global_config() or {}
        cfg["username"] = data.get("user", {}).get("username", username)
        cfg["access_token"] = data["access_token"]
        cfg["origin_url"] = server_url
        if not self.save_global_config(cfg):
            print("Authenticated, but the token could not be saved")
            return False

        print(f"Signed in as {cfg['username']} using {private_path.name}.")
        return True

    # ------------------------------------------------------------------
    # Search
    #
    # Code search is scoped to one repository (the linked one by default),
    # because a store-wide content scan would read gigabytes per query.
    # ------------------------------------------------------------------

    def search_repos(self, query=None, owner=None, limit=50):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        params = {"limit": limit}
        if query:
            params["q"] = query
        if owner:
            params["owner"] = owner
        try:
            response = requests.get(f"{server_url}/api/search/repositories", params=params,
                                    headers=self.get_auth_headers(), timeout=60)
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Search failed: {self._response_error_message(response)}")
            return False

        data = response.json()
        repos = data.get("repositories", [])
        if not repos:
            print("No repositories matched.")
            return True
        print(f"{'ID':<18} {'NAME':<28} {'OWNER':<14} DESCRIPTION")
        for r in repos:
            flag = " [archived]" if r.get("is_archived") else ""
            print(f"{r['id']:<18} {(r['name'] or '')[:27]:<28} {(r.get('owner') or '-')[:13]:<14} "
                  f"{(r.get('description') or '')[:40]}{flag}")
        if data.get("truncated"):
            print(f"... {data['total']} total; raise --limit to see more")
        return True

    def search_commits_cmd(self, query=None, author=None, all_repos=False, limit=50):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        params = {"limit": limit}
        if query:
            params["q"] = query
        if author:
            params["author"] = author
        if not all_repos:
            repo_id = (self.load_config() or {}).get("repo_id")
            if repo_id:
                params["repository_id"] = repo_id

        try:
            response = requests.get(f"{server_url}/api/search/commits", params=params,
                                    headers=self.get_auth_headers(), timeout=60)
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Search failed: {self._response_error_message(response)}")
            return False

        commits = response.json().get("commits", [])
        if not commits:
            print("No commits matched.")
            return True
        for c in commits:
            when = (c.get("created_at") or "")[:19].replace("T", " ")
            print(f"{c['id'][:12]}  {when:<20} {(c.get('author') or '-'):<12} "
                  f"{(c.get('repository_name') or '-')[:18]:<19} {(c.get('message') or '')[:48]}")
        return True

    def search_code_cmd(self, query, branch=None, path=None, regex=False,
                        case_sensitive=False, repo=None, limit=200):
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False

        repo_id = repo or (self.load_config() or {}).get("repo_id")
        if not repo_id:
            print("Code search needs a repository.")
            print("Run this inside a linked repo, or pass --repo <id>.")
            return False

        params = {"repository_id": repo_id, "q": query, "limit": limit}
        if branch:
            params["branch"] = branch
        if path:
            params["path"] = path
        if regex:
            params["regex"] = "true"
        if case_sensitive:
            params["case_sensitive"] = "true"

        try:
            response = requests.get(f"{server_url}/api/search/code", params=params,
                                    headers=self.get_auth_headers(), timeout=120)
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Search failed: {self._response_error_message(response)}")
            return False

        data = response.json()
        results = data.get("results", [])
        if not results:
            print(f"No matches. Scanned {data.get('files_scanned', 0)} file(s).")
            return True

        for f in results:
            print(f"\n{f['path']}  ({f['match_count']})")
            for m in f["matches"]:
                print(f"  {m['line']:>6}: {m['text'].rstrip()[:110]}")

        print(f"\n{data['total_matches']} match(es) in {len(results)} file(s); "
              f"{data['files_scanned']} scanned, {data['files_skipped']} skipped")
        if data.get("truncated"):
            print("Results were truncated; narrow the query or use --path.")
        return True

    # ------------------------------------------------------------------
    # Administrative operations
    #
    # Backup, restore and schema checks live in server/tools/ rather than here:
    # they need filesystem access on the server host, so putting them in the
    # developer client would only ever produce a confusing error. What belongs
    # here is what an admin can genuinely do remotely.
    # ------------------------------------------------------------------

    def admin_lockouts(self):
        """Show locked accounts and where failed sign-ins are coming from."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.get(
                f"{server_url}/api/admin/security/lockouts",
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to read lockouts: {self._response_error_message(response)}")
            return False

        data = response.json()
        p = data.get("policy", {})
        print(f"Policy: {p.get('max_account_failures')} failures per account in "
              f"{p.get('account_window_minutes')} min -> {p.get('lockout_minutes')} min lockout; "
              f"{p.get('max_ip_failures')} per address")
        print()

        locked = data.get("locked_accounts", [])
        if not locked:
            print("No accounts are locked.")
        else:
            print(f"{'USERNAME':<24} {'REMAINING':<12} {'FAILURES':<10} LAST IP")
            for l in locked:
                mins = max(1, round(l["seconds_remaining"] / 60))
                print(f"{l['username'][:23]:<24} {str(mins) + ' min':<12} "
                      f"{l['failed_count']:<10} {l.get('last_ip') or '-'}")

        ips = data.get("recent_failures_by_ip", [])
        if ips:
            print()
            print(f"Failed attempts in the last {data.get('window_minutes')} minutes, by source:")
            for row in ips[:10]:
                print(f"  {row['ip_address']:<24} {row['failures']}")
        return True

    def admin_unlock(self, username):
        """Clear a lockout early."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.post(
                f"{server_url}/api/admin/security/unlock",
                json={"username": username},
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Unlock failed: {self._response_error_message(response)}")
            return False
        data = response.json()
        if data.get("was_locked"):
            print(f"{username} unlocked; its failure history was cleared too.")
        else:
            print(f"{username} was not locked. Any failure history has been cleared.")
        return True

    def admin_schema(self):
        """Report whether the live schema matches the models."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.get(
                f"{server_url}/api/admin/schema",
                headers=self.get_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to read schema health: {self._response_error_message(response)}")
            return False

        s = response.json().get("schema", {})
        print(f"  tables expected : {s.get('tables_expected')}")
        print(f"  tables present  : {s.get('tables_present')}")
        print(f"  missing tables  : {len(s.get('missing_tables') or [])}")
        print(f"  missing columns : {len(s.get('missing_columns') or [])}")
        if s.get("healthy"):
            print("\n  healthy - the live schema matches the models.")
            return True
        print()
        for t in s.get("missing_tables") or []:
            print(f"  missing table : {t}")
        for c in (s.get("missing_columns") or [])[:20]:
            print(f"  missing column: {c['table']}.{c['column']}")
        print("\n  DRIFT - run 'py -3.11 tools/schema.py --check' on the server.")
        return False

    def admin_backups(self):
        """List backups recorded on the server."""
        server_url = self._resolve_server_url()
        if not self.ensure_authenticated():
            return False
        try:
            response = requests.get(
                f"{server_url}/api/admin/backups",
                headers=self.get_auth_headers(), timeout=60,
            )
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return False
        if response.status_code != 200:
            print(f"Failed to list backups: {self._response_error_message(response)}")
            return False

        data = response.json()
        rows = data.get("backups", [])
        print(f"Destination: {data.get('destination')}")
        print()
        if not rows:
            print("No backups recorded.")
            print("On the server: py -3.11 tools/backup.py --create")
            return True

        def mb(n):
            return f"{(n or 0) / 1024 / 1024:.1f} MB"

        print(f"{'WHEN':<21} {'LABEL':<16} {'DB':<11} {'BLOBS':<11} {'OBJECTS':<9} OK")
        for b in rows:
            when = (b.get("created_at") or "")[:19].replace("T", " ")
            print(f"{when:<21} {b['label'][:15]:<16} {mb(b['database_bytes']):<11} "
                  f"{mb(b['blob_bytes']):<11} {b['blob_count']:<9} "
                  f"{'yes' if b['verified'] else 'NO'}")
        return True

    def _authed_post(self, url, json=None, params=None, timeout=30):
        """POST with auth header; on 401, prompt login once and retry."""
        if not self.ensure_authenticated():
            resp = requests.Response()
            resp.status_code = 401
            resp._content = b'{"detail":"Authentication required"}'
            resp.headers["Content-Type"] = "application/json"
            return resp

        headers = self.get_auth_headers()
        try:
            resp = requests.post(url, json=json, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise

        if resp.status_code == 401:
            had_token = bool(headers.get("Authorization"))
            print("Authentication required. Attempting interactive login...")
            if self.login():
                headers = self.get_auth_headers()
                try:
                    resp = requests.post(url, json=json, params=params, headers=headers, timeout=timeout)
                except requests.exceptions.RequestException:
                    pass
            elif had_token:
                print(
                    f"\nTip: Token may be for a different server than {self._resolve_server_url()}.\n"
                    f"     Run: fox login   (credentials: {self.global_config_file})"
                )
        return resp

    def _authed_get(self, url, params=None, timeout=30):
        """GET with auth header; on 401, prompt login once and retry."""
        if not self.ensure_authenticated():
            resp = requests.Response()
            resp.status_code = 401
            resp._content = b'{"detail":"Authentication required"}'
            resp.headers["Content-Type"] = "application/json"
            return resp

        headers = self.get_auth_headers()
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise

        if resp.status_code == 401:
            had_token = bool(headers.get("Authorization"))
            print("Authentication required. Attempting interactive login...")
            if self.login():
                headers = self.get_auth_headers()
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
                except requests.exceptions.RequestException:
                    pass
            elif had_token:
                print(
                    f"\nTip: Token may be for a different server than {self._resolve_server_url()}.\n"
                    f"     Run: fox login   (credentials: {self.global_config_file})"
                )
        return resp

    def _authed_delete(self, url, timeout=30):
        """DELETE with auth header; on 401, prompt login once and retry."""
        if not self.ensure_authenticated():
            resp = requests.Response()
            resp.status_code = 401
            resp._content = b'{"detail":"Authentication required"}'
            resp.headers["Content-Type"] = "application/json"
            return resp

        headers = self.get_auth_headers()
        try:
            resp = requests.delete(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise

        if resp.status_code == 401:
            had_token = bool(headers.get("Authorization"))
            print("Authentication required. Attempting interactive login...")
            if self.login():
                headers = self.get_auth_headers()
                try:
                    resp = requests.delete(url, headers=headers, timeout=timeout)
                except requests.exceptions.RequestException:
                    pass
            elif had_token:
                print(
                    f"\nTip: Token may be for a different server than {self._resolve_server_url()}.\n"
                    f"     Run: fox login   (credentials: {self.global_config_file})"
                )
        return resp
    
    def save_global_config(self, config):
        """Save global FoxNest credentials"""
        self.global_config_dir.mkdir(exist_ok=True)
        
        try:
            with open(self.global_config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            return True
        except Exception as e:
            print(f"Error: Could not save global config: {e}")
            return False
    
    def set_global_username(self, username):
        """Set global username for all Fox repositories"""
        config = self.load_global_config()
        config["username"] = username
        
        if self.save_global_config(config):
            print(f"Global username set to: {username}")
            print(f"Config saved to: {self.global_config_file}")
            return True
        return False
    
    def resolve_user_path(self, user_path):
        """Map a path the user typed onto one relative to the repository root.

        Commands run from the root, so `fox add main.py` issued from `src/`
        means `src/main.py`. Absolute paths are left alone but made relative to
        the root when they fall inside it, so both forms end up in the same
        shape the index and the server expect.
        """
        if not user_path or not self.path_prefix:
            return user_path

        candidate = Path(user_path)
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(Path.cwd().resolve()).as_posix()
            except ValueError:
                return user_path  # outside the repository; leave it to fail loudly

        # "." from a subfolder means that subfolder, not the whole repository.
        text = str(user_path).replace("\\", "/")
        if text in (".", "./"):
            return self.path_prefix
        return f"{self.path_prefix}/{text.lstrip('./')}"

    def check_repository(self, command_name="command"):
        """Check if current directory is a Fox repository"""
        if not self.is_initialized():
            current_dir = Path.cwd().name
            print(f"Fatal: {current_dir} not a repository, run 'fox init' to initialize a new repository")
            return False
        return True
    
    def get_origin_url(self):
        """Get the origin URL from config (local or global)"""
        local = self._read_config_raw()
        if local and local.get("origin_url"):
            return local["origin_url"]

        global_config = self.load_global_config()
        return global_config.get("origin_url")
    
    def set_origin(self, origin_url, is_global=False):
        """Set the origin URL for the repository"""
        # Validate URL format
        if not origin_url.startswith(('http://', 'https://')):
            # Assume it's an IP:port and add http://
            if ':' in origin_url:
                origin_url = f"http://{origin_url}"
            else:
                origin_url = f"http://{origin_url}:33333"
        
        if is_global:
            # Set global origin
            config = self.load_global_config()
            config["origin_url"] = origin_url
            if self.save_global_config(config):
                print(f"Global origin set to: {origin_url}")
                print(f"Config saved to: {self.global_config_file}")
                return True
            return False
        else:
            # Set local origin
            if not self.check_repository("set origin"):
                return False
            
            config = self.load_config()
            config["origin_url"] = origin_url
            config["server_url"] = origin_url
            self.server_url = origin_url
            self.save_config(config)
            
            print(f"Origin set to: {origin_url}")
            return True
    
    def set_global_origin(self, origin_url):
        """Set global origin URL for all Fox repositories"""
        return self.set_origin(origin_url, is_global=True)

    def set_repo_id(self, repo_id: str) -> bool:
        """Set local repository repo_id (links this working copy to server repo)."""
        if not self.check_repository("set repo-id"):
            return False
        rid = (repo_id or "").strip()
        if not rid:
            print("❌ repo-id cannot be empty")
            return False
        if len(rid) < 8:
            print("❌ repo-id looks too short")
            return False
        config = self.load_config()
        if not config:
            return False
        config["repo_id"] = rid
        self.save_config(config)
        print(f"Repository linked to remote repo_id: {rid}")
        return True

    def _sync_server_url(self, config=None):
        """Keep server_url aligned with origin_url (fixes stale IP after server move)."""
        if config is not None:
            url = config.get("origin_url") or config.get("server_url")
            if not url:
                url = self.get_origin_url() or self.server_url
            if config.get("origin_url"):
                config["server_url"] = config["origin_url"]
                url = config["origin_url"]
            elif url:
                config["server_url"] = url
        else:
            url = self.get_origin_url() or self.server_url

        if url:
            self.server_url = url
        return url

    @staticmethod
    def _repo_name_variants(repo_name):
        """Names to try when matching server repos (handles accidental spaces)."""
        if not repo_name:
            return []
        raw = str(repo_name)
        stripped = raw.strip()
        variants = []
        for value in (raw, stripped):
            if value and value not in variants:
                variants.append(value)
        return variants

    def _resolve_server_url(self, config=None):
        """Which server to talk to.

        FOXNEST_SERVER overrides everything, so a host can point one workspace at
        a different server without rewriting the user's global config.
        """
        env_server = (os.environ.get("FOXNEST_SERVER") or "").strip()
        if env_server:
            return env_server.rstrip("/")
        if config is not None:
            return config.get("origin_url") or config.get("server_url") or self.server_url
        origin = self.get_origin_url()
        return origin if origin else self.server_url
    
    def init(self, username=None, repo_name=None):
        """Initialize a new Fox repository"""
        if self.fox_dir.exists():
            print("Repository already initialized!")
            return False
        
        # Try to get username from global config if not provided
        if not username:
            global_config = self.load_global_config()
            username = global_config.get("username")
            
            if username:
                print(f"Using global username: {username}")
            else:
                username = input("Enter username: ")
                
                # Offer to save username globally
                save_global = input("Save username globally for future repositories? (y/n): ").lower()
                if save_global == 'y':
                    self.set_global_username(username)
        
        if not repo_name:
            repo_name = input("Enter repository name: ")
        
        # Create .fox directory structure
        self.fox_dir.mkdir()
        self.staging_dir.mkdir()
        self.objects_dir.mkdir()
        self.packs_dir.mkdir()  # For pack files
        self.refs_dir.mkdir()
        self.heads_dir.mkdir()
        self.tags_dir.mkdir()
        
        # Create initial configuration
        config = {
            "username": username,
            "repo_name": repo_name,
            "server_url": self.server_url,
            "repo_id": None,
            "initialized_at": datetime.now().isoformat()
        }
        
        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)
        
        # Initialize empty commits file
        with open(self.commits_file, "w") as f:
            json.dump([], f)

        # Initialize default branch and HEAD
        self._write_ref(self.heads_dir / "main", "")
        with open(self.head_file, "w") as f:
            f.write("ref: refs/heads/main")

        # Seed a .foxignore so build output and archives never enter the store.
        # Never clobber one the user already wrote.
        ignore_path = Path(IGNORE_FILENAME)
        if not ignore_path.exists():
            try:
                ignore_path.write_text(DEFAULT_FOXIGNORE_TEMPLATE, encoding="utf-8")
                print(f"Created {IGNORE_FILENAME} (edit it to change what Fox tracks)")
            except OSError as exc:
                print(f"Warning: could not create {IGNORE_FILENAME}: {exc}")

        print(f"Initialized Fox repository for {username}/{repo_name}")
        print("Use 'fox add <files>' to add files and 'fox commit' to commit changes")
        return True
    
    def is_initialized(self):
        """Check if repository is initialized"""
        return self.fox_dir.exists() and self.config_file.exists()

    def _read_config_raw(self):
        """Read .fox/config.json without normalization (avoids recursion in URL helpers)."""
        if not self.config_file.exists():
            return None
        try:
            with open(self.config_file, "r") as f:
                return json.load(f)
        except Exception:
            return None
    
    def load_config(self):
        """Load repository configuration"""
        if not self.is_initialized():
            print("Not a Fox repository! Run 'fox init' first.")
            return None
        
        with open(self.config_file, "r") as f:
            config = json.load(f)

        if config.get("repo_name") and config["repo_name"] != str(config["repo_name"]).strip():
            print(f"⚠️  Trimming spaces from repository name: {config['repo_name']!r}")
            config["repo_name"] = str(config["repo_name"]).strip()
            self.save_config(config)

        self._sync_server_url(config)
        if config.get("origin_url") and config.get("server_url") != config.get("origin_url"):
            self.save_config(config)

        return config
    
    def save_config(self, config):
        """Save repository configuration"""
        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)

    def ensure_refs(self):
        """Ensure refs/heads and refs/tags exist and HEAD points to a branch"""
        self.refs_dir.mkdir(exist_ok=True)
        self.heads_dir.mkdir(exist_ok=True)
        self.tags_dir.mkdir(exist_ok=True)

        head_content = ""
        if self.head_file.exists():
            head_content = self.head_file.read_text().strip()

        if head_content.startswith("ref:"):
            return

        # Legacy HEAD stored commit id; promote to main branch
        legacy_commit = head_content
        main_ref = self.heads_dir / "main"
        if not main_ref.exists():
            self._write_ref(main_ref, legacy_commit)
        if not self.head_file.exists() or not head_content.startswith("ref:"):
            self.head_file.write_text("ref: refs/heads/main")

    def _write_ref(self, ref_path: Path, value: str):
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ref_path, "w") as f:
            f.write(value or "")

    def _read_ref(self, ref_path: Path):
        if not ref_path.exists():
            return None
        return ref_path.read_text().strip() or None

    def get_head_ref(self):
        if not self.head_file.exists():
            return None
        content = self.head_file.read_text().strip()
        if content.startswith("ref:"):
            return content.split("ref:")[-1].strip()
        return None

    def get_current_branch(self):
        ref = self.get_head_ref()
        if ref and ref.startswith("refs/heads/"):
            return ref.split("refs/heads/")[-1]
        return None

    def get_head_commit_id(self):
        ref = self.get_head_ref()
        if ref:
            return self._read_ref(self.fox_dir / ref)
        if self.head_file.exists():
            return self.head_file.read_text().strip() or None
        return None

    def update_head_commit(self, commit_id: str):
        ref = self.get_head_ref()
        if ref:
            self._write_ref(self.fox_dir / ref, commit_id)
            return
        if self.head_file.exists():
            self.head_file.write_text(commit_id)

    def set_head_ref(self, branch_name: str):
        self.head_file.write_text(f"ref: refs/heads/{branch_name}")
    
    def compress_data(self, data):
        """Compress data using zlib - Git-like compression"""
        if isinstance(data, str):
            data = data.encode('utf-8')
        return zlib.compress(data, self.compression_level)
    
    def decompress_data(self, compressed_data):
        """Decompress zlib compressed data"""
        return zlib.decompress(compressed_data)
    
    def calculate_delta(self, base_content, new_content):
        """
        Calculate delta between two file versions using unified diff
        Returns delta object with base hash and diff data
        """
        if isinstance(base_content, bytes):
            base_content = base_content.decode('utf-8', errors='ignore')
        if isinstance(new_content, bytes):
            new_content = new_content.decode('utf-8', errors='ignore')
        
        # Generate unified diff
        base_lines = base_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        
        delta = list(difflib.unified_diff(base_lines, new_lines, lineterm=''))
        
        return {
            'type': 'delta',
            'delta_data': ''.join(delta)
        }
    
    def apply_delta(self, base_content, delta_data):
        """Apply delta to base content to reconstruct new content"""
        if isinstance(base_content, bytes):
            base_content = base_content.decode('utf-8', errors='ignore')
        
        base_lines = base_content.splitlines(keepends=True)
        delta_lines = delta_data.splitlines(keepends=True)
        
        # Apply the unified diff
        result_lines = []
        delta_idx = 0
        base_idx = 0
        
        # Skip diff headers
        while delta_idx < len(delta_lines) and not delta_lines[delta_idx].startswith('@@'):
            delta_idx += 1
        
        while delta_idx < len(delta_lines):
            line = delta_lines[delta_idx]
            
            if line.startswith('@@'):
                # Parse range information
                delta_idx += 1
                continue
            elif line.startswith('-'):
                # Line removed from base (skip in base)
                base_idx += 1
            elif line.startswith('+'):
                # Line added in new version
                result_lines.append(line[1:])
            else:
                # Context line (same in both)
                if base_idx < len(base_lines):
                    result_lines.append(base_lines[base_idx])
                    base_idx += 1
            
            delta_idx += 1
        
        return ''.join(result_lines)
    
    def store_object_compressed(self, content, file_hash):
        """
        Store object with compression in objects directory
        Uses 2-character prefix directory like Git for better file system performance
        """
        # Create subdirectory based on first 2 chars of hash (like Git)
        obj_dir = self.objects_dir / file_hash[:2]
        obj_dir.mkdir(exist_ok=True)
        
        obj_path = obj_dir / file_hash[2:]
        
        # Compress and store
        if isinstance(content, str):
            content = content.encode('utf-8')
        
        compressed = self.compress_data(content)
        
        with open(obj_path, 'wb') as f:
            f.write(compressed)
        
        return obj_path
    
    def load_object_compressed(self, file_hash):
        """Load and decompress object from objects directory"""
        obj_path = self.objects_dir / file_hash[:2] / file_hash[2:]
        
        if not obj_path.exists():
            # Fallback to old flat structure
            old_path = self.objects_dir / file_hash
            if old_path.exists():
                with open(old_path, 'rb') as f:
                    return f.read()
            return None
        
        with open(obj_path, 'rb') as f:
            compressed = f.read()
        
        return self.decompress_data(compressed)
    
    def find_similar_object(self, new_hash, file_path):
        """
        Find a similar object for delta compression
        Looks for previous version of same file
        """
        delta_cache = self.load_delta_cache()
        
        # Check if we have a previous version of this file
        if file_path in delta_cache:
            return delta_cache[file_path].get('base_hash')
        
        return None
    
    def load_delta_cache(self):
        """Load delta cache that tracks file version relationships"""
        if not self.delta_cache_file.exists():
            return {}
        
        try:
            with open(self.delta_cache_file, 'r') as f:
                return json.load(f)
        except:
            return {}
    
    def save_delta_cache(self, cache):
        """Save delta cache"""
        with open(self.delta_cache_file, 'w') as f:
            json.dump(cache, f, indent=2)
    
    def update_delta_cache(self, file_path, file_hash):
        """Update delta cache with new file version"""
        cache = self.load_delta_cache()
        
        old_hash = cache.get(file_path, {}).get('current_hash')
        
        cache[file_path] = {
            'current_hash': file_hash,
            'base_hash': old_hash  # Previous version becomes base for delta
        }
        
        self.save_delta_cache(cache)
    
    def pack_objects(self):
        """
        Pack loose objects into compressed pack file (like git gc)
        Reduces storage and improves performance
        """
        loose_objects = []
        
        print("  Collecting loose objects...")
        
        # Collect all loose objects
        for root, dirs, files in os.walk(self.objects_dir):
            for file in files:
                if file != '.gitkeep':
                    obj_path = Path(root) / file
                    # Reconstruct hash from directory structure
                    parent = obj_path.parent.name
                    if len(parent) == 2:
                        full_hash = parent + file
                    else:
                        full_hash = file
                    loose_objects.append((full_hash, obj_path))
        
        if len(loose_objects) < self.pack_threshold:
            print("  Not enough objects to pack, skipping...")
            return  # Not enough objects to pack
        
        print(f"  Packing {len(loose_objects)} objects...")
        
        # Create pack file
        pack_id = hashlib.sha256(str(datetime.now()).encode()).hexdigest()[:12]
        pack_path = self.packs_dir / f"pack-{pack_id}.pack"
        index_path = self.packs_dir / f"pack-{pack_id}.idx"
        
        pack_data = {}
        
        for obj_hash, obj_path in loose_objects:
            try:
                with open(obj_path, 'rb') as f:
                    pack_data[obj_hash] = base64.b64encode(f.read()).decode()
                
                # Remove loose object after packing
                obj_path.unlink()
            except Exception as e:
                print(f"Warning: Could not pack object {obj_hash}: {e}")
        
        # Write pack file (compressed JSON)
        pack_json = json.dumps(pack_data)
        compressed_pack = self.compress_data(pack_json)
        
        with open(pack_path, 'wb') as f:
            f.write(compressed_pack)
        
        # Write index file for quick lookups
        index = {
            'pack_file': pack_path.name,
            'object_count': len(pack_data),
            'objects': list(pack_data.keys()),
            'created_at': datetime.now().isoformat()
        }
        
        with open(index_path, 'w') as f:
            json.dump(index, f, indent=2)
        
        print(f"Packed {len(pack_data)} objects into {pack_path.name}")
        
        # Clean up empty directories
        for root, dirs, files in os.walk(self.objects_dir, topdown=False):
            for dir_name in dirs:
                dir_path = Path(root) / dir_name
                try:
                    if not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except:
                    pass
    
    def get_file_hash(self, filepath):
        """Generate hash for a file"""
        with open(filepath, "rb") as f:
            content = f.read()
        return hashlib.sha256(content).hexdigest()[:16]
    
    def load_index(self):
        """Load the git-like index of tracked files"""
        if not self.index_file.exists():
            return {}
        try:
            with open(self.index_file, "r") as f:
                return json.load(f)
        except:
            return {}
    
    def save_index(self, index):
        """Save the git-like index of tracked files"""
        with open(self.index_file, "w") as f:
            json.dump(index, f, indent=2)
    
    def update_index_from_commit(self, commit_files):
        """Update index from committed files"""
        index = {}
        for file_hash, file_data in commit_files.items():
            file_path = file_data["path"]
            if Path(file_path).exists():
                stat = Path(file_path).stat()
                index[file_path] = {
                    "hash": file_hash,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size
                }
        self.save_index(index)
    
    def init_index_from_last_commit(self, verbose=False):
        """Initialize index from the last commit for fast tracking"""
        if not self.commits_file.exists():
            return
        
        try:
            commits = self.load_commits()
            if not commits:
                return
            head_commit_id = self.get_head_commit_id()
            latest_commit = self.get_commit_by_id(head_commit_id) if head_commit_id else commits[-1]
            commit_files = latest_commit.get("files", {})
            self.update_index_from_commit(commit_files)
            if verbose:
                print(f"Initialized index with {len(commit_files)} tracked files")
        except:
            pass
    
    def get_modified_files_fast(self):
        """Fast git-like detection of modified files"""
        index = self.load_index()
        
        # If no index exists, initialize it from last commit
        if not index:
            self.init_index_from_last_commit()
            index = self.load_index()
        
        if not index:
            # No commits exist yet, this is a new repository
            return []
        
        modified_files = []
        
        for file_path, file_info in index.items():
            file_path_obj = Path(file_path)
            
            # Check if file still exists
            if not file_path_obj.exists():
                modified_files.append(file_path_obj)
                continue
            
            # Quick check: compare size and modification time
            try:
                stat = file_path_obj.stat()
                if stat.st_size != file_info["size"] or stat.st_mtime != file_info["mtime"]:
                    # File might be modified, check hash to be sure
                    current_hash = self.get_file_hash(file_path_obj)
                    if current_hash != file_info["hash"]:
                        modified_files.append(file_path_obj)
            except:
                # If we can't stat the file, consider it modified
                modified_files.append(file_path_obj)
        
        return modified_files
    
    def get_modified_files_comprehensive(self):
        """Comprehensive detection of modified files including nested directories"""
        index = self.load_index()
        
        # If no index exists, initialize it from last commit
        if not index:
            self.init_index_from_last_commit()
            index = self.load_index()
        
        if not index:
            # No commits exist yet, this is a new repository
            return []
        
        modified_files = []
        all_files = self.get_all_files()
        
        # Check all existing files for modifications
        for file_path_obj in all_files:
            file_path = str(file_path_obj)
            
            # If file is tracked, check if it's modified
            if file_path in index:
                file_info = index[file_path]
                
                # Always check hash for comprehensive detection
                try:
                    current_hash = self.get_file_hash(file_path_obj)
                    if current_hash != file_info["hash"]:
                        modified_files.append(file_path_obj)
                except:
                    # If we can't read the file, consider it modified
                    modified_files.append(file_path_obj)
        
        # Check for deleted files (in index but not on disk)
        for file_path in index.keys():
            if not Path(file_path).exists():
                modified_files.append(Path(file_path))
        
        return modified_files
    
    def get_all_files(self):
        """Every trackable file in the working directory, honouring .foxignore.

        Falls back to the built-in defaults when the repository has no .foxignore.
        Note this no longer skips every dotfile: real configuration such as
        .env.example or .foxignore itself is trackable, and the ignore rules decide.
        """
        matcher = load_ignore_matcher(".")
        all_files = []

        for file_path in Path(".").rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.as_posix()
            if relative.startswith("./"):
                relative = relative[2:]
            if not matcher.is_ignored(relative):
                all_files.append(file_path)

        return all_files

    def report_ignored_files(self, limit=10):
        """List what .foxignore is currently excluding, for `fox status`."""
        matcher = load_ignore_matcher(".")
        hits = []
        for file_path in Path(".").rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.as_posix()
            pattern = matcher.matched_rule(relative)
            if pattern and not relative.startswith(".fox/"):
                hits.append((relative, pattern))
                if len(hits) >= limit:
                    break
        return hits
    
    def get_last_commit_files(self):
        """Get file states from the last commit"""
        try:
            commits = self.load_commits()
            if not commits:
                return {}
            head_commit_id = self.get_head_commit_id()
            latest_commit = self.get_commit_by_id(head_commit_id) if head_commit_id else commits[-1]
            commit_files = latest_commit.get("files", {})
            
            # Convert from hash-based storage to path-based for easy lookup
            path_to_hash = {}
            for file_hash, file_data in commit_files.items():
                path_to_hash[file_data["path"]] = file_hash
            
            return path_to_hash
        except:
            return {}
    
    def is_file_modified(self, filepath):
        """Check if file has been modified since last commit"""
        current_hash = self.get_file_hash(filepath)
        last_commit_files = self.get_last_commit_files()
        
        # If file wasn't in last commit, it's new (modified)
        if str(filepath) not in last_commit_files:
            return True
        
        # If hash is different, it's modified
        return last_commit_files[str(filepath)] != current_hash
    
    def get_modified_files(self):
        """Get all files that have been modified since last commit"""
        modified_files = []
        for filepath in self.get_all_files():
            if self.is_file_modified(filepath):
                modified_files.append(filepath)
        return modified_files

    def load_commits(self):
        if not self.commits_file.exists():
            return []
        with open(self.commits_file, "r") as f:
            return json.load(f)

    def get_commit_by_id(self, commit_id: str):
        if not commit_id:
            return None
        for commit in self.load_commits():
            if commit.get("id") == commit_id:
                return commit
        return None

    def _get_commit_parents(self, commit):
        if not commit:
            return []
        parents = commit.get("parents")
        if parents:
            return [p for p in parents if p]
        parent = commit.get("parent")
        return [parent] if parent else []

    def _get_ancestors_with_depth(self, commit_id: str):
        depth = {}
        if not commit_id:
            return depth
        queue = [(commit_id, 0)]
        depth[commit_id] = 0
        while queue:
            current, dist = queue.pop(0)
            commit = self.get_commit_by_id(current)
            for parent_id in self._get_commit_parents(commit):
                if parent_id and parent_id not in depth:
                    depth[parent_id] = dist + 1
                    queue.append((parent_id, dist + 1))
        return depth

    def _find_merge_base(self, commit_a: str, commit_b: str):
        if not commit_a or not commit_b:
            return None
        depth_a = self._get_ancestors_with_depth(commit_a)
        depth_b = self._get_ancestors_with_depth(commit_b)
        common = set(depth_a.keys()) & set(depth_b.keys())
        if not common:
            return None
        return min(common, key=lambda cid: depth_a[cid] + depth_b[cid])

    def _commit_tree(self, commit_id: str):
        commit = self.get_commit_by_id(commit_id)
        if not commit:
            return {}
        tree = {}
        for file_hash, file_info in commit.get("files", {}).items():
            if not isinstance(file_info, dict):
                continue
            file_path = file_info.get("path")
            content_b64 = file_info.get("content", "")
            if not file_path:
                continue
            try:
                tree[file_path] = base64.b64decode(content_b64)
            except Exception:
                tree[file_path] = b""
        return tree

    def _try_decode_text(self, content):
        if content is None:
            return None
        try:
            return content.decode("utf-8")
        except Exception:
            return None

    def _merge_text(self, base, ours, theirs):
        base_text = self._try_decode_text(base) or ""
        ours_text = self._try_decode_text(ours) or ""
        theirs_text = self._try_decode_text(theirs) or ""
        if ours_text == theirs_text:
            return ours_text.encode("utf-8"), False
        if base_text == ours_text:
            return theirs_text.encode("utf-8"), False
        if base_text == theirs_text:
            return ours_text.encode("utf-8"), False
        merged = (
            "<<<<<<< OURS\n"
            f"{ours_text}"
            "\n=======\n"
            f"{theirs_text}"
            "\n>>>>>>> THEIRS\n"
        )
        return merged.encode("utf-8"), True

    def _merge_trees(self, base, ours, theirs):
        merged = {}
        conflicts = []
        paths = set(base.keys()) | set(ours.keys()) | set(theirs.keys())
        for path in sorted(paths):
            base_content = base.get(path)
            our_content = ours.get(path)
            their_content = theirs.get(path)

            if our_content == their_content:
                if our_content is not None:
                    merged[path] = our_content
                continue

            if base_content == our_content:
                if their_content is not None:
                    merged[path] = their_content
                continue

            if base_content == their_content:
                if our_content is not None:
                    merged[path] = our_content
                continue

            if self._try_decode_text(our_content) is not None and self._try_decode_text(their_content) is not None:
                merged_content, has_conflict = self._merge_text(base_content, our_content, their_content)
                merged[path] = merged_content
                if has_conflict:
                    conflicts.append(path)
            else:
                if our_content is not None:
                    merged[path] = our_content
                conflicts.append(path)

        return merged, conflicts

    def list_branches(self):
        if not self.check_repository("branch"):
            return False
        self.ensure_refs()
        branches = []
        for ref in self.heads_dir.glob("*"):
            branches.append(ref.name)
        current = self.get_current_branch()
        for name in sorted(branches):
            marker = "*" if name == current else " "
            print(f"{marker} {name}")
        return True

    def create_branch(self, branch_name, from_commit=None, from_branch=None, checkout=False):
        if not self.check_repository("branch"):
            return False
        self.ensure_refs()
        branch_ref = self.heads_dir / branch_name
        if branch_ref.exists():
            print(f"Branch already exists: {branch_name}")
            return False
        head_commit = from_commit
        if from_branch and not head_commit:
            head_commit = self.get_local_branch_head_commit_id(from_branch)
            if not head_commit:
                print(f"Source branch not found or empty: {from_branch!r}")
                return False
        if not head_commit:
            head_commit = self.get_head_commit_id()
        self._write_ref(branch_ref, head_commit or "")

        # Create branch on server if repo is linked
        config = self.load_config()
        if config and config.get("repo_id"):
            try:
                server_url = self._resolve_server_url()
                preferred_actor = None
                if head_commit:
                    head_commit_data = self.get_commit_by_id(head_commit)
                    if head_commit_data:
                        preferred_actor = head_commit_data.get('author')
                actor = self._resolve_actor_username(preferred_actor)
                payload = {"name": branch_name, "from_commit": head_commit, "actor_username": actor}
                if from_branch:
                    payload["from_branch"] = from_branch.replace("\\", "/")
                resp = self._authed_post(
                    f"{server_url}/api/repository/{config['repo_id']}/branches",
                    json=payload,
                )
                if resp.status_code not in (200, 201):
                    if branch_ref.exists():
                        branch_ref.unlink()
                    print(f"Error: server branch creation failed ({resp.status_code}): {resp.text}")
                    print("Local branch creation reverted to avoid unsynced branch state.")
                    return False
            except Exception as e:
                if branch_ref.exists():
                    branch_ref.unlink()
                print(f"Error: could not create branch on server: {e}")
                print("Local branch creation reverted to avoid unsynced branch state.")
                return False

        if checkout:
            return self.checkout_branch(branch_name)
        print(f"Created branch {branch_name}")
        return True

    def delete_branch(self, branch_name):
        if not self.check_repository("branch"):
            return False
        self.ensure_refs()
        branch_ref = self.heads_dir / branch_name
        if not branch_ref.exists():
            print(f"Branch not found: {branch_name}")
            return False
        if branch_name == self.get_current_branch():
            print("Cannot delete the current branch")
            return False
        branch_ref.unlink()

        config = self.load_config()
        if config and config.get("repo_id"):
            try:
                server_url = self._resolve_server_url()
                self._authed_delete(
                    f"{server_url}/api/repository/{config['repo_id']}/branches/{branch_name}",
                    timeout=30,
                )
            except Exception:
                pass

        print(f"Deleted branch {branch_name}")
        return True

    def _fetch_server_branches_index(self):
        """Return dict branch_name -> head_commit_id from server, or None on failure."""
        config = self.load_config()
        if not config or not config.get("repo_id"):
            return None
        server_url = self._resolve_server_url()
        try:
            resp = self._authed_get(
                f"{server_url}/api/repository/{config['repo_id']}/branches",
                timeout=60,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("success"):
                return None
            out = {}
            for b in data.get("branches") or []:
                name = b.get("name")
                hid = b.get("head_commit_id") or b.get("head")
                if name and hid:
                    out[name] = hid
            return out
        except Exception:
            return None

    def _resolve_compare_ref(self, ref: str, branches_index: dict) -> str:
        """Resolve a user ref to a full commit id: branch tip (server) or local commit id / prefix."""
        if not ref or not ref.strip():
            return None
        ref = ref.strip()
        if branches_index:
            if ref in branches_index:
                return branches_index[ref]
            lower_map = {k.lower(): v for k, v in branches_index.items()}
            if ref.lower() in lower_map:
                return lower_map[ref.lower()]
        exact = self.get_commit_by_id(ref)
        if exact and exact.get("id"):
            return exact["id"]
        matches = []
        for commit in self.load_commits():
            cid = commit.get("id") or ""
            if cid.startswith(ref):
                matches.append(cid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"Ambiguous commit prefix '{ref}' matches {len(matches)} commits. Use a longer prefix or full id.")
            return None
        return ref if len(ref) >= 7 else None

    def compare(self, from_ref: str, to_ref: str, path=None, as_json=False):
        """
        Compare repository trees at two branch tips or commits using GET /compare.
        from_ref / to_ref: branch name (server) or commit id (local exact or unique prefix).
        """
        if not self.check_repository("compare"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Fatal: No repo_id in .fox/config.json. Run 'fox push' or 'fox set repo-id …' first.")
            return False
        origin_url = self.get_origin_url()
        if not origin_url:
            print("Fatal: No origin set. Use 'fox set origin <url>' first.")
            return False

        branches_index = self._fetch_server_branches_index()
        if branches_index is None:
            print("Warning: Could not list remote branches; commit-id resolution may be limited.")

        from_commit = self._resolve_compare_ref(from_ref, branches_index or {})
        to_commit = self._resolve_compare_ref(to_ref, branches_index or {})
        if not from_commit:
            print(f"Could not resolve 'from' ref: {from_ref!r}")
            return False
        if not to_commit:
            print(f"Could not resolve 'to' ref: {to_ref!r}")
            return False

        server_url = self._resolve_server_url()
        repo_id = config["repo_id"]
        params = {
            "from_commit": from_commit,
            "to_commit": to_commit,
        }
        if path:
            params["path"] = path.replace("\\", "/")

        url = f"{server_url}/api/repository/{repo_id}/compare?{urllib.parse.urlencode(params)}"
        try:
            resp = self._authed_get(url, timeout=120)
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            return False

        if resp.status_code != 200:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            print(f"Compare failed ({resp.status_code}): {detail}")
            return False

        try:
            data = resp.json()
        except Exception:
            print("Invalid JSON from server")
            return False

        if not data.get("success"):
            print(f"Compare failed: {data}")
            return False

        if as_json:
            print(json.dumps(data, indent=2))
            return True

        files = data.get("files") or []
        print("")
        print(f"Compare: {from_ref!r} ({from_commit[:12]}…) → {to_ref!r} ({to_commit[:12]}…)")
        if path:
            print(f"Path filter: {path}")
        print(f"Changed files: {len(files)}")
        print("")

        max_rows_per_file = 80
        for entry in files:
            fp = entry.get("file_path", "?")
            status = entry.get("status", "?")
            print(f"--- {fp}  ({status})")
            if entry.get("is_binary"):
                print("    [binary or large — diff omitted]")
                print("")
                continue
            diff = entry.get("diff") or {}
            rows = diff.get("rows") or []
            if not rows:
                print("    (no line-level diff rows)")
                print("")
                continue
            shown = 0
            for row in rows:
                if shown >= max_rows_per_file:
                    print(f"    … ({len(rows) - shown} more rows; use --json for full output)")
                    break
                typ = row.get("type", "")
                prev = row.get("previous", "")
                cur = row.get("current", "")
                if typ == "same":
                    print(f"    {prev}")
                elif typ == "added":
                    print(f"  + {cur}")
                elif typ == "removed":
                    print(f"  - {prev}")
                elif typ == "changed":
                    print(f"  - {prev}")
                    print(f"  + {cur}")
                else:
                    print(f"    {typ}: {prev!r} → {cur!r}")
                shown += 1
            stats = diff.get("stats") or {}
            if stats:
                print(f"    stats: +{stats.get('added', 0)} -{stats.get('removed', 0)} ~{stats.get('unchanged', 0)}")
            print("")
        if not files:
            print("No file-level differences between the two commits (for the given path filter).")
        return True

    def file_history(self, path, branch=None, lines_range=None, as_json=False):
        """
        List file revision history from the server (GET /file-history).
        Optional lines_range: inclusive 1-based 'START-END' or 'START:END' to include line_history events.
        """
        if not self.check_repository("file-history"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Fatal: No repo_id in .fox/config.json. Run 'fox push' or 'fox set repo-id …' first.")
            return False
        if not self.get_origin_url():
            print("Fatal: No origin set. Use 'fox set origin <url>' first.")
            return False

        line_start = None
        line_end = None
        if lines_range:
            raw = lines_range.strip()
            sep = None
            if "-" in raw:
                sep = "-"
            elif ":" in raw:
                sep = ":"
            if not sep:
                print("Use --lines START-END or START:END (1-based inclusive), e.g. 12-40")
                return False
            left, right = raw.split(sep, 1)
            try:
                line_start = int(left.strip())
                line_end = int(right.strip())
            except ValueError:
                print("Line range must be two integers, e.g. --lines 12-40")
                return False

        server_url = self._resolve_server_url()
        repo_id = config["repo_id"]
        norm = path.replace("\\", "/")
        params = {"path": norm, "limit": "500", "follow_renames": "true"}
        if branch:
            params["branch"] = branch
        if line_start is not None:
            params["line_start"] = str(line_start)
            params["line_end"] = str(line_end)

        url = f"{server_url}/api/repository/{repo_id}/file-history?{urllib.parse.urlencode(params)}"
        try:
            resp = self._authed_get(url, timeout=120)
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            return False

        if resp.status_code != 200:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            print(f"file-history failed ({resp.status_code}): {detail}")
            return False

        try:
            data = resp.json()
        except Exception:
            print("Invalid JSON from server")
            return False

        if not data.get("success"):
            print(f"file-history failed: {data}")
            return False

        if as_json:
            print(json.dumps(data, indent=2))
            return True

        versions = data.get("versions") or []
        line_hist = data.get("line_history")
        lr = data.get("line_range")
        print("")
        print(f"File history: {norm}")
        if branch:
            print(f"  Branch scope: {branch}")
        print(f"  Revisions in this page: {len(versions)} (server limit/pagination may apply)")
        print("")
        for v in versions:
            cid = (v.get("commit_id") or "")[:12]
            msg = (v.get("message") or "").replace("\n", " ")
            author = v.get("author") or "?"
            ts = v.get("timestamp") or ""
            ct = v.get("change_type") or ""
            print(f"  {cid}…  {ts}  {author}  [{ct}]")
            print(f"      {msg}")
        if line_hist is not None:
            print("")
            if lr:
                print(f"  Line range {lr.get('start')}–{lr.get('end')}: commits where that slice changed")
            else:
                print("  Line-range history")
            for row in line_hist:
                cid = (row.get("commit_id") or "")[:12]
                msg = (row.get("message") or "").replace("\n", " ")
                excerpt = row.get("lines_excerpt") or ""
                ind = "    "
                print(f"{ind}{cid}…  {msg}")
                for ln in excerpt.splitlines() or ["(empty)"]:
                    print(f"{ind}  | {ln}")
                if row.get("lines_truncated"):
                    print(f"{ind}  … (truncated)")
        elif lines_range:
            print("")
            print("  (No line_history in response; check server supports --lines.)")
        return True

    def _list_local_branch_heads(self):
        """Return mapping branch_name -> head commit id for all local refs under refs/heads."""
        self.ensure_refs()
        heads = {}
        if not self.heads_dir.exists():
            return heads
        for p in self.heads_dir.iterdir():
            if p.is_file():
                cid = self._read_ref(p)
                if cid:
                    heads[p.name] = cid
        return heads

    def delete_commit(self, ref, branch=None, local_only=False, expected_head=None):
        """
        Remove a branch-tip commit.

        - If the commit was never pushed (not present on the server for this repo), only local
          refs and commits.json are updated — no server call.
        - If it exists on the server as the branch tip, POST /commits/drop removes it from the DB
          and all branch tips that pointed at it are rewound to the parent.
        - Default branch on the server requires management permission; other branches require write.

        Use --local-only only for commits that do not exist on the server; otherwise the command
        refuses to avoid desynchronizing from the remote.
        """
        if not self.check_repository("delete-commit"):
            return False

        self.ensure_refs()
        cid = self._resolve_commit_ref_local(ref)
        if not cid:
            print(f"Commit not found: {ref!r}")
            return False

        co = self.get_commit_by_id(cid)
        if not co:
            print(f"Commit not found in local commits.json: {ref!r}")
            return False

        parents = self._get_commit_parents(co)
        if len(parents) > 1:
            print("Cannot delete a merge commit (multiple parents) with this command.")
            return False
        parent_id = parents[0] if parents else None
        if not parent_id:
            print("Cannot delete the initial (root) commit.")
            return False

        for c in self.load_commits():
            oid = c.get("id")
            if not oid or oid == cid:
                continue
            for p in self._get_commit_parents(c):
                if p == cid:
                    print(
                        "A newer local commit builds on this one. Drop newer branch tips first, "
                        "or use rollback instead of deleting history."
                    )
                    return False

        branch = branch or self.get_current_branch() or "main"
        heads_map = self._list_local_branch_heads()
        local_tip = heads_map.get(branch)
        if local_tip != cid:
            print(
                f"Branch {branch!r} does not point at this commit locally "
                f"(tip is {(local_tip or '')[:12] or 'none'}…)."
            )
            print("Checkout that branch or pass --branch <name> where this commit is the tip.")
            return False

        config = self.load_config() or {}
        repo_id = config.get("repo_id")
        origin_url = self.get_origin_url()
        server_url = self._resolve_server_url() if origin_url else None

        on_server = False
        server_branch_is_tip = False
        if repo_id and server_url and not local_only:
            try:
                br = self._authed_get(
                    f"{server_url}/api/repository/{repo_id}/branches",
                    timeout=60,
                )
            except requests.exceptions.RequestException as e:
                print(f"Could not reach server to list branches: {e}")
                return False

            server_tip = None
            if br.status_code == 200:
                bdata = br.json()
                for row in bdata.get("branches") or []:
                    if row.get("name") == branch:
                        server_tip = row.get("head_commit_id")
                        break
            elif br.status_code in (401, 403):
                print("Authentication or permission failed while listing remote branches.")
                return False
            else:
                print(f"Server returned {br.status_code} when listing branches; aborting.")
                return False

            try:
                cr = self._authed_get(
                    f"{server_url}/api/repository/{repo_id}/commits/{cid}",
                    params={"full": "false"},
                    timeout=60,
                )
            except requests.exceptions.RequestException as e:
                print(f"Could not reach server to verify commit: {e}")
                return False

            on_server = cr.status_code == 200
            if cr.status_code not in (200, 404):
                if cr.status_code in (401, 403):
                    print("Authentication or permission failed while checking the commit on the server.")
                    return False
                print(f"Server returned {cr.status_code} when checking commit; aborting.")
                return False

            server_branch_is_tip = bool(server_tip and server_tip == cid)

        if local_only:
            if on_server and server_branch_is_tip:
                print(
                    "This commit is still the tip on the server. Omit --local-only to delete it on the server "
                    "and locally, or run without deleting if you only meant to fix local state."
                )
                return False

        if repo_id and server_url and not local_only:
            if on_server:
                if not server_branch_is_tip:
                    print(
                        "On the server this commit is not the tip of the selected branch. "
                        "Only branch tips can be removed (rewinds to parent)."
                    )
                    return False
                body = {
                    "commit_id": cid,
                    "branch": branch,
                }
                if expected_head:
                    body["expected_head_commit_id"] = expected_head
                else:
                    body["expected_head_commit_id"] = cid
                try:
                    dr = self._authed_post(
                        f"{server_url}/api/repository/{repo_id}/commits/drop",
                        json=body,
                        timeout=120,
                    )
                except requests.exceptions.RequestException as e:
                    print(f"delete-commit request failed: {e}")
                    return False
                if dr.status_code != 200:
                    try:
                        detail = dr.json()
                    except Exception:
                        detail = dr.text
                    print(f"Server refused delete-commit ({dr.status_code}): {detail}")
                    return False
                try:
                    djson = dr.json()
                except Exception:
                    djson = {}
                if not djson.get("success"):
                    print(f"Server delete-commit failed: {djson}")
                    return False
                print(
                    f"Server: removed tip {cid[:12]}… rewound branches {', '.join(djson.get('rewound_branches') or [])} "
                    f"to {(djson.get('new_head_commit_id') or '')[:12]}…"
                )
            else:
                print("Commit not found on server — applying local-only removal (never pushed or fully local).")

        # Local store: move any branch tip that still references cid to parent
        for bname, tip in list(self._list_local_branch_heads().items()):
            if tip == cid:
                self._write_ref(self.heads_dir / bname, parent_id)

        commits = [c for c in self.load_commits() if c.get("id") != cid]
        with open(self.commits_file, "w") as f:
            json.dump(commits, f, indent=2)

        if self.get_current_branch() == branch:
            self.update_head_commit(parent_id)

        parent_commit = self.get_commit_by_id(parent_id)
        if parent_commit:
            self.extract_commit_files(parent_commit)
            self.update_index_from_commit(parent_commit.get("files", {}))

        if self.get_current_branch() == branch and self.staging_dir.exists():
            for staging_file in self.staging_dir.glob("*"):
                try:
                    staging_file.unlink()
                except OSError:
                    pass

        print(f"Locally removed commit {cid[:12]}… branch {branch!r} now at {parent_id[:12]}…")
        return True

    def checkout_branch(self, branch_name):
        if not self.check_repository("checkout"):
            return False
        self.ensure_refs()
        branch_ref = self.heads_dir / branch_name
        if not branch_ref.exists():
            print(f"Branch not found: {branch_name}")
            return False

        target_commit = self._read_ref(branch_ref)
        if target_commit:
            commit = self.get_commit_by_id(target_commit)
            if commit:
                # Remove files not in target commit
                target_paths = {info["path"] for info in commit.get("files", {}).values() if isinstance(info, dict)}
                current_paths = set(self.get_last_commit_files().keys())
                for removed_path in current_paths - target_paths:
                    try:
                        Path(removed_path).unlink()
                    except Exception:
                        pass
                self.extract_commit_files(commit)
                self.update_index_from_commit(commit.get("files", {}))

        self.set_head_ref(branch_name)
        print(f"Switched to branch {branch_name}")
        return True

    def _normalize_repo_relative_path(self, raw_path: str) -> str:
        """Normalize user path to repo-relative forward slashes."""
        p = (raw_path or "").strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p

    def _find_commit_file_entry(self, commit: dict, normalized_path: str) -> Tuple[Optional[str], Optional[dict]]:
        """Return (file_hash_key, file_info dict) for a path in a local commit snapshot, or (None, None)."""
        for file_hash, file_info in (commit.get("files") or {}).items():
            if not isinstance(file_info, dict):
                continue
            p = (file_info.get("path") or "").replace("\\", "/")
            if p == normalized_path:
                return str(file_hash), file_info
        return None, None

    def _write_working_file_from_commit_entry(self, file_hash: str, file_info: dict) -> bool:
        """Write one file from commit storage to the working tree (same rules as extract_commit_files)."""
        file_path = Path(file_info["path"])
        content = file_info.get("content")
        if content is None:
            print(f"Missing content for {file_path}")
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                content_bytes = base64.b64decode(content)
                with open(file_path, "wb") as f:
                    f.write(content_bytes)
            except Exception:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else str(content))
            print(f"Restored: {file_path}")
            return True
        except Exception as e:
            print(f"Failed to write {file_path}: {e}")
            return False

    def _merge_index_for_paths(self, path_to_hash: Dict[str, str]) -> None:
        """Update or add index entries for paths after writing files; keep other index entries."""
        index = self.load_index() or {}
        for path_str, file_hash in path_to_hash.items():
            p = Path(path_str)
            if p.exists():
                try:
                    stat = p.stat()
                    index[path_str] = {
                        "hash": file_hash,
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    }
                except OSError:
                    pass
        self.save_index(index)

    def _resolve_local_commit_for_ref(self, from_ref: str) -> Tuple[Optional[dict], Optional[str]]:
        """
        Resolve from_ref to a commit dict in commits.json.
        Returns (commit, branch_name_if_from_local_branch_ref) for messaging / server fallback.
        """
        ref = (from_ref or "").strip()
        if not ref:
            return None, None

        self.ensure_refs()
        safe_branch = ref.replace("\\", "/")
        local_branch_file = self.heads_dir / safe_branch
        if local_branch_file.exists():
            cid = self._read_ref(local_branch_file)
            if cid:
                co = self.get_commit_by_id(cid)
                if co:
                    return co, safe_branch

        cid = self._resolve_commit_ref_local(ref)
        if cid:
            co = self.get_commit_by_id(cid)
            if co:
                return co, None
        return None, None

    def _server_branch_names(self) -> Optional[List[str]]:
        config = self.load_config() or {}
        repo_id = config.get("repo_id")
        if not repo_id or not self.get_origin_url():
            return None
        server_url = self._resolve_server_url()
        try:
            resp = self._authed_get(f"{server_url}/api/repository/{repo_id}/branches", timeout=30)
        except requests.exceptions.RequestException:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        return [b.get("name") for b in (data.get("branches") or []) if b.get("name")]

    def _checkout_paths_from_server_branch(self, branch_name: str, norm_paths: List[str]) -> int:
        """Fetch each path from GET /file for the given server branch. Returns count of paths restored."""
        config = self.load_config() or {}
        repo_id = config.get("repo_id")
        if not repo_id or not self.get_origin_url():
            print("Server checkout-files requires repo_id and origin (fox set origin / fox set repo-id).")
            return 0
        server_url = self._resolve_server_url()
        names = self._server_branch_names()
        if names is not None and branch_name not in names:
            print(f"Branch {branch_name!r} not found on server. Available: {', '.join(sorted(names))}")
            return 0

        path_to_hash: Dict[str, str] = {}
        for norm in norm_paths:
            params = {"path": norm, "branch": branch_name}
            url = f"{server_url}/api/repository/{repo_id}/file?{urllib.parse.urlencode(params)}"
            try:
                resp = self._authed_get(url, timeout=60)
            except requests.exceptions.RequestException as e:
                print(f"Request failed for {norm}: {e}")
                continue
            if resp.status_code != 200:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                print(f"Server could not provide {norm!r} ({resp.status_code}): {detail}")
                continue
            try:
                payload = resp.json()
            except Exception:
                print(f"Invalid JSON for {norm!r}")
                continue
            if not payload.get("success"):
                print(f"Server error for {norm!r}: {payload}")
                continue
            finfo = payload.get("file") or {}
            content = finfo.get("content")
            if content is None and not finfo.get("is_binary"):
                print(f"No content in response for {norm!r}")
                continue
            file_path = Path(norm)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if finfo.get("is_binary"):
                    raw = base64.b64decode(content)
                    with open(file_path, "wb") as f:
                        f.write(raw)
                else:
                    with open(file_path, "w", encoding="utf-8", newline="") as f:
                        f.write(content if isinstance(content, str) else str(content))
                print(f"Restored from server ({branch_name}): {file_path}")
                fh = finfo.get("hash") or self.get_file_hash(file_path)
                path_to_hash[norm] = fh
            except Exception as e:
                print(f"Failed to write {norm!r}: {e}")

        if path_to_hash:
            self._merge_index_for_paths(path_to_hash)
        return len(path_to_hash)

    def checkout_files(self, from_ref: str, paths: List[str], prefer_server: bool = False) -> bool:
        """
        Restore specific working-tree files from another branch or commit without switching branches.

        - Local: ``from_ref`` is a branch with a ref under ``.fox/refs/heads/``, or a commit id / prefix
          present in ``commits.json``. Files are taken from that commit snapshot.
        - Server: if no local snapshot is available (or ``--server``), ``from_ref`` must be a branch
          name on the server; each path is fetched with GET /file?branch=…
        """
        if not self.check_repository("checkout-files"):
            return False
        if not paths:
            print("No file paths given.")
            return False

        ref = (from_ref or "").strip()
        if not ref:
            print("Missing --from <branch-or-commit>.")
            return False

        norm_paths = []
        for raw in paths:
            n = self._normalize_repo_relative_path(raw)
            if n:
                norm_paths.append(n)
        if not norm_paths:
            print("No valid paths after normalization.")
            return False

        if prefer_server:
            n_srv = self._checkout_paths_from_server_branch(ref, norm_paths)
            if not n_srv:
                return False
            print(f"Checked out {n_srv} path(s) from server branch {ref!r} (current branch unchanged).")
            return True

        commit, hint_branch = self._resolve_local_commit_for_ref(ref)
        if commit:
            path_to_hash: Dict[str, str] = {}
            missing = []
            for norm in norm_paths:
                fh, fi = self._find_commit_file_entry(commit, norm)
                if not fi:
                    missing.append(norm)
                    continue
                if self._write_working_file_from_commit_entry(fh or "", fi):
                    path_to_hash[norm] = fh or self.get_file_hash(Path(norm))
            if missing:
                print("Not found in source commit (skipped): " + ", ".join(missing))
            if not path_to_hash:
                print("No files were restored. Check paths and source ref.")
                return False
            self._merge_index_for_paths(path_to_hash)
            src = hint_branch or ref
            print(f"Checked out {len(path_to_hash)} path(s) from {src!r} (current branch unchanged).")
            return True

        # Local ref not resolved — try server by branch name when repo is linked
        cfg = self.load_config() or {}
        if cfg.get("repo_id") and self.get_origin_url():
            names = self._server_branch_names()
            if names is None or ref in names:
                n_srv = self._checkout_paths_from_server_branch(ref, norm_paths)
                if n_srv:
                    print(f"Checked out {n_srv} path(s) from server branch {ref!r} (current branch unchanged).")
                    return True

        print(
            f"Could not resolve {ref!r} to a local branch/commit in commits.json, "
            "and server branch lookup did not succeed."
        )
        print("Hints: use a branch that exists locally or a full commit id from `fox log`; "
              "for server-only branches use the exact server branch name, or pass --server with that branch.")
        return False

    def checkout_files_server_commit(self, from_branch: str, paths: List[str]) -> bool:
        """Copy files from one branch to the current branch on the server (creates a server-side commit)."""
        if not self.check_repository("checkout-files"):
            return False
        if not paths:
            print("No file paths given.")
            return False
        config = self.load_config() or {}
        repo_id = config.get("repo_id")
        if not repo_id or not self.get_origin_url():
            print("Server commit requires repo_id and origin (fox set origin / fox set repo-id).")
            return False
        current_branch = self.get_current_branch()
        if not current_branch:
            print("Cannot determine current branch. Switch to a branch first.")
            return False
        norm_paths = [self._normalize_repo_relative_path(p) for p in paths]
        norm_paths = [p for p in norm_paths if p]
        if not norm_paths:
            print("No valid paths after normalization.")
            return False
        server_url = self._resolve_server_url()
        payload = {
            "source_branch": from_branch.strip(),
            "target_branch": current_branch,
            "paths": norm_paths,
        }
        try:
            resp = self._authed_post(
                f"{server_url}/api/repository/{repo_id}/branches/copy-files",
                json=payload,
                timeout=60,
            )
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            return False
        if resp.status_code == 200:
            data = resp.json()
            print(f"Server committed {len(norm_paths)} path(s) from '{from_branch}' onto '{current_branch}'.")
            cid = data.get("commit_id") or data.get("head_commit_id")
            if cid:
                print(f"  commit: {cid}")
            return True
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        print(f"Server error ({resp.status_code}): {detail}")
        return False

    def merge_branch(self, source_branch):
        if not self.check_repository("merge"):
            return False
        self.ensure_refs()
        current_branch = self.get_current_branch()
        if not current_branch:
            print("Cannot merge in detached HEAD state")
            return False
        if source_branch == current_branch:
            print("Source and target branches are the same")
            return False

        source_ref = self.heads_dir / source_branch
        if not source_ref.exists():
            print(f"Branch not found: {source_branch}")
            return False

        target_commit = self._read_ref(self.heads_dir / current_branch)
        source_commit = self._read_ref(source_ref)

        base_commit = self._find_merge_base(target_commit, source_commit)
        base_tree = self._commit_tree(base_commit)
        target_tree = self._commit_tree(target_commit)
        source_tree = self._commit_tree(source_commit)

        merged_tree, conflicts = self._merge_trees(base_tree, target_tree, source_tree)

        for file_path, content in merged_tree.items():
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(content)

        if conflicts:
            print("Merge conflicts detected in:")
            for path in conflicts:
                print(f"  {path}")
            print("Resolve conflicts, then commit the merge.")
            return False

        merge_message = f"Merge branch '{source_branch}' into '{current_branch}'"
        self.commit(merge_message, parents=[target_commit, source_commit])
        print("Merge completed")
        return True

    def _resolve_commit_ref_local(self, ref: str) -> Optional[str]:
        """Resolve commit id or unique local prefix to full commit id."""
        if not ref or not str(ref).strip():
            return None
        ref = str(ref).strip()
        if self.get_commit_by_id(ref):
            return ref
        matches = []
        for commit in self.load_commits():
            cid = commit.get("id") or ""
            if cid.startswith(ref):
                matches.append(cid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"Ambiguous commit prefix {ref!r} matches {len(matches)} commits; use a longer id.")
            return None
        return None

    def _is_ancestor_commit_local(self, ancestor_id: str, head_id: str) -> bool:
        """True if ancestor_id appears in the parent history of head_id (local commits.json)."""
        if not ancestor_id or not head_id:
            return False
        from collections import deque

        q = deque([head_id])
        seen = set()
        while q:
            cid = q.popleft()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if cid == ancestor_id:
                return True
            co = self.get_commit_by_id(cid)
            if not co:
                continue
            for p in self._get_commit_parents(co):
                if p:
                    q.append(p)
        return False

    def _merge_pulled_commits_into_local(self, commits: List[dict]) -> None:
        """Append commits from server pull/rollback into commits.json (dedupe by id)."""
        if not commits:
            return
        local_commits = []
        if self.commits_file.exists():
            with open(self.commits_file, "r") as f:
                local_commits = json.load(f)
        existing = {c.get("id") for c in local_commits if c.get("id")}
        for commit in commits:
            cid = commit.get("id")
            if cid and cid not in existing:
                normalized_files = {}
                for file_hash, file_info in commit.get("files", {}).items():
                    if isinstance(file_info, dict):
                        content_b64 = file_info.get("content", "")
                        file_path = file_info.get("path")
                    else:
                        content_b64 = file_info
                        file_path = None
                    try:
                        decoded_content = base64.b64decode(content_b64)
                    except Exception:
                        continue
                    self.store_object_compressed(decoded_content, file_hash)
                    normalized_files[file_hash] = {
                        "path": file_path if file_path else file_hash,
                        "content": content_b64,
                    }
                commit["files"] = normalized_files
                local_commits.append(commit)
                existing.add(cid)
        with open(self.commits_file, "w") as f:
            json.dump(local_commits, f, indent=2)

    def rollback(self, commit_id):
        """
        Move the current branch back to the tree at the given ancestor commit.

        - With a linked server: POST /rollback/branch (adds a rollback commit on the same branch).
        - Offline: moves the branch ref directly to that commit (local-only).
        """
        if not self.check_repository("rollback"):
            return False

        self.ensure_refs()
        current_branch = self.get_current_branch() or "main"
        prev_head = self.get_head_commit_id()
        if not prev_head:
            print("Cannot rollback: branch has no HEAD commit.")
            return False

        target_id = self._resolve_commit_ref_local(commit_id)
        if not target_id:
            print(f"Commit not found: {commit_id!r}")
            return False

        if not self._is_ancestor_commit_local(target_id, prev_head):
            print("That commit is not an ancestor of your current branch tip (or history is missing locally).")
            print("Try: fox pull   then retry, or use the full commit id that exists on this branch.")
            return False

        if target_id == prev_head:
            print("Already at that commit.")
            return True

        target_commit = self.get_commit_by_id(target_id)
        config = self.load_config()

        if config and config.get("repo_id"):
            server_url = self._resolve_server_url()
            try:
                resp = self._authed_post(
                    f"{server_url}/api/repository/{config['repo_id']}/rollback/branch",
                    json={
                        "branch": current_branch,
                        "target_commit_id": target_id,
                        "expected_head_commit_id": prev_head,
                        "summary": f"rollback(branch): {current_branch} to {target_id[:8]} (fox CLI)",
                    },
                    timeout=120,
                )
            except requests.exceptions.RequestException as e:
                print(f"Rollback request failed: {e}")
                return False

            if resp.status_code != 200:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                print(f"Server rollback failed ({resp.status_code}): {detail}")
                return False

            try:
                data = resp.json()
            except Exception:
                print("Invalid JSON from server")
                return False
            if not data.get("success"):
                print(f"Server rollback failed: {data}")
                return False

            new_head = data.get("new_commit_id")
            if not new_head:
                print("Server did not return new_commit_id")
                return False

            try:
                pull_resp = self._authed_get(
                    f"{server_url}/api/repository/{config['repo_id']}/pull",
                    params={"since": prev_head, "branch": current_branch},
                    timeout=120,
                )
                if pull_resp.status_code == 200:
                    pull_data = pull_resp.json()
                    if pull_data.get("success"):
                        pulled = pull_data.get("commits") or []
                        self._merge_pulled_commits_into_local(pulled)
            except requests.exceptions.RequestException:
                pass

            self._write_ref(self.heads_dir / current_branch, new_head)
            self.update_head_commit(new_head)

            try:
                branches_resp = self._authed_get(
                    f"{server_url}/api/repository/{config['repo_id']}/branches",
                    timeout=30,
                )
                if branches_resp.status_code == 200:
                    bd = branches_resp.json()
                    if bd.get("success"):
                        for b in bd.get("branches") or []:
                            if b.get("name") == current_branch:
                                hid = b.get("head_commit_id") or b.get("head")
                                if hid:
                                    self._write_ref(self.heads_dir / current_branch, hid)
                                    self.update_head_commit(hid)
                                    new_head = hid
                                break
            except Exception:
                pass

            head_commit = self.get_commit_by_id(new_head)
            if not head_commit:
                print(
                    f"Rollback recorded on server (new head {new_head[:12]}…). "
                    f"Local commit cache is missing that commit — run: fox pull"
                )
                return True

            self.extract_commit_files(head_commit)
            self.update_index_from_commit(head_commit.get("files", {}))
            print(f"✓ Rolled back branch {current_branch!r} on server and updated working tree.")
            print(f"  New tip: {new_head[:12]}… (tree matches ancestor {target_id[:12]}…)")
            return True

        self._write_ref(self.heads_dir / current_branch, target_id)
        self.update_head_commit(target_id)
        self.extract_commit_files(target_commit)
        self.update_index_from_commit(target_commit.get("files", {}))
        print(f"✓ Rolled back local branch {current_branch!r} to commit {target_id[:12]}… (offline repo).")
        return True

    def list_tags(self):
        if not self.check_repository("tag"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()
        response = self._authed_get(
            f"{server_url}/api/repository/{config['repo_id']}/tags",
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            for tag in data.get("tags", []):
                print(f"{tag['name']} -> {tag['commit_id']}")
            return True
        print(f"Error listing tags: HTTP {response.status_code}")
        return False

    def create_tag(self, name, commit_id=None, message=None):
        if not self.check_repository("tag"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        payload = {"name": name, "commit_id": commit_id, "message": message}
        server_url = self._resolve_server_url()
        response = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/tags",
            json=payload,
            timeout=30,
        )
        if response.status_code == 200:
            print(f"Created tag {name}")
            return True
        print(f"Error creating tag: {response.text}")
        return False

    def delete_tag(self, name):
        if not self.check_repository("tag"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()
        response = self._authed_delete(
            f"{server_url}/api/repository/{config['repo_id']}/tags/{name}",
            timeout=30,
        )
        if response.status_code == 200:
            print(f"Deleted tag {name}")
            return True
        print(f"Error deleting tag: {response.text}")
        return False

    def list_releases(self):
        if not self.check_repository("release"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()
        response = self._authed_get(
            f"{server_url}/api/repository/{config['repo_id']}/releases",
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            for release in data.get("releases", []):
                print(f"{release['version']} ({release.get('tag')})")
            return True
        print(f"Error listing releases: HTTP {response.status_code}")
        return False

    def create_release(self, version, tag=None, title=None, notes=None):
        if not self.check_repository("release"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        payload = {"version": version, "tag": tag, "title": title, "notes": notes}
        server_url = self._resolve_server_url()
        response = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/releases",
            json=payload,
            timeout=30,
        )
        if response.status_code == 200:
            print(f"Created release {version}")
            return True
        print(f"Error creating release: {response.text}")
        return False

    def list_pull_requests(self, status=None):
        if not self.check_repository("pr"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        params = {"status": status} if status else {}
        server_url = self._resolve_server_url()
        response = self._authed_get(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests",
            params=params,
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            for pr in data.get("pull_requests", []):
                print(f"#{pr['id']} {pr['title']} [{pr['status']}] {pr['source_branch']} -> {pr['target_branch']}")
            return True
        print(f"Error listing pull requests: HTTP {response.status_code}")
        return False

    def create_pull_request(self, title, source_branch, target_branch, description=None):
        if not self.check_repository("pr"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        payload = {
            "title": title,
            "description": description,
            "source_branch": source_branch,
            "target_branch": target_branch
        }
        server_url = self._resolve_server_url()
        response = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests",
            json=payload,
            timeout=30,
        )
        if response.status_code == 200:
            pr = response.json().get("pull_request", {})
            print(f"Created PR #{pr.get('id')}: {pr.get('title')}")
            return True
        print(f"Error creating PR: {response.text}")
        return False

    def close_pull_request(self, pr_id):
        if not self.check_repository("pr"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()
        response = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests/{pr_id}/close",
            timeout=30,
        )
        if response.status_code == 200:
            print(f"Closed PR #{pr_id}")
            return True
        print(f"Error closing PR: {response.text}")
        return False

    def merge_pull_request(self, pr_id):
        if not self.check_repository("pr"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()
        response = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests/{pr_id}/merge",
            json={},
            timeout=60
        )
        if response.status_code == 200:
            data = response.json()
            print(f"Merged PR #{pr_id} into {data.get('merge_commit_id')}")
            return True
        if response.status_code == 409:
            data = response.json()
            print("Merge conflicts detected:")
            for path in data.get("conflicts", []):
                print(f"  {path}")
            session_id = data.get("session_id")
            if session_id:
                self._store_merge_conflict_session(pr_id, session_id)
                print("\nTo resolve in CLI:")
                print(f"  fox pr resolve {pr_id} --session {session_id}")
                print(f"  fox pr resolve {pr_id} --session {session_id} --submit  # after editing .resolved files\n")
            return False
        print(f"Error merging PR: {response.text}")
        return False

    def _load_merge_conflicts_state(self):
        if not self.merge_conflicts_file.exists():
            return {}
        try:
            with open(self.merge_conflicts_file, "r") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_merge_conflicts_state(self, state: dict):
        try:
            self.fox_dir.mkdir(exist_ok=True)
            with open(self.merge_conflicts_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _store_merge_conflict_session(self, pr_id: int, session_id: int):
        state = self._load_merge_conflicts_state()
        state[str(pr_id)] = {"session_id": int(session_id), "updated_at": datetime.utcnow().isoformat()}
        self._save_merge_conflicts_state(state)

    def _get_merge_conflict_session(self, pr_id: int):
        state = self._load_merge_conflicts_state()
        entry = state.get(str(pr_id)) or {}
        sid = entry.get("session_id")
        return int(sid) if sid is not None else None

    def pr_resolve(self, pr_id: int, session_id: Optional[int] = None, submit: bool = False, directory: Optional[str] = None):
        """
        Download merge conflicts (base/ours/theirs) to disk and optionally submit resolutions.

        Workflow:
          1) fox pr merge <id>  (if conflict prints session id)
          2) fox pr resolve <id> --session <sid>
             -> edit *.resolved files under .fox/merge-conflicts/pr_<id>_session_<sid>/
          3) fox pr resolve <id> --session <sid> --submit
        """
        if not self.check_repository("pr"):
            return False
        config = self.load_config()
        if not config or not config.get("repo_id"):
            print("Repository not linked to server. Push first.")
            return False
        server_url = self._resolve_server_url()

        if session_id is None:
            session_id = self._get_merge_conflict_session(pr_id)
        if session_id is None:
            print("Missing session id. Use:")
            print(f"  fox pr resolve {pr_id} --session <session_id>")
            return False

        base_dir = Path(directory) if directory else (self.fox_dir / "merge-conflicts" / f"pr_{pr_id}_session_{session_id}")
        base_dir.mkdir(parents=True, exist_ok=True)

        # Fetch conflict bundle
        resp = self._authed_get(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests/{pr_id}/merge-conflicts/{session_id}",
            timeout=120,
        )
        if resp.status_code != 200:
            print(f"Error fetching merge conflicts ({resp.status_code}): {resp.text}")
            return False
        data = resp.json()
        files = data.get("files", []) or []
        if not files:
            print("No conflict files returned.")
            return False

        # Write base/ours/theirs + resolved template
        for f in files:
            rel = f.get("path")
            if not rel:
                continue
            safe_path = rel.replace("\\", "/")
            target_dir = base_dir / os.path.dirname(safe_path)
            target_dir.mkdir(parents=True, exist_ok=True)

            def write_b64(suffix: str, b64: Optional[str]):
                if b64 is None:
                    return
                try:
                    content = base64.b64decode(b64.encode("utf-8"))
                except Exception:
                    content = b""
                with open(base_dir / f"{safe_path}.{suffix}", "wb") as out:
                    out.write(content)

            write_b64("base", f.get("base_b64"))
            write_b64("ours", f.get("ours_b64"))
            write_b64("theirs", f.get("theirs_b64"))

            # resolved starts from suggested if present, else ours
            resolved_b64 = f.get("suggested_b64") or f.get("ours_b64")
            if resolved_b64 is not None:
                write_b64("resolved", resolved_b64)

        print(f"Downloaded conflicts to: {base_dir}")
        print("Edit the *.resolved files, then submit with:")
        print(f"  fox pr resolve {pr_id} --session {session_id} --submit\n")

        if not submit:
            return True

        # Build resolutions from *.resolved files
        resolutions = {}
        for f in files:
            rel = f.get("path")
            if not rel:
                continue
            safe_path = rel.replace("\\", "/")
            resolved_path = base_dir / f"{safe_path}.resolved"
            if not resolved_path.exists():
                print(f"Missing resolved file: {resolved_path}")
                return False
            content = resolved_path.read_bytes()
            resolutions[safe_path] = base64.b64encode(content).decode("utf-8")

        submit_resp = self._authed_post(
            f"{server_url}/api/repository/{config['repo_id']}/pull-requests/{pr_id}/merge-resolve/{session_id}",
            json={"resolutions": resolutions},
            timeout=300,
        )
        if submit_resp.status_code != 200:
            print(f"Resolve failed ({submit_resp.status_code}): {submit_resp.text}")
            return False
        out = submit_resp.json()
        if out.get("success"):
            print(f"Resolved and merged PR #{pr_id}. Merge commit: {out.get('merge_commit_id')}")
            return True
        print(f"Resolve failed: {out}")
        return False
    
    def add(self, files, add_all=False):
        """Add files to staging area"""
        if not self.check_repository("add"):
            return False

        # Ensure staging directory exists
        self.staging_dir.mkdir(exist_ok=True)

        # Handle --all flag or when files contains "."
        if add_all or (files and "." in files):
            # Get current index
            index = self.load_index()
            
            if not index:
                # No tracked files yet - stage all files like git add .
                print("Staging all files (first time)...")
                all_files = self.get_all_files()
                if not all_files:
                    print("No files to add")
                    return True
                files = [str(f) for f in all_files]
            else:
                # We have tracked files - stage both modified tracked files AND untracked files
                modified_files = self.get_modified_files_comprehensive()
                all_files = self.get_all_files()
                tracked_files = set(index.keys())
                all_file_paths = {str(f) for f in all_files}
                
                # Include both modified files and untracked files
                untracked_files = all_file_paths - tracked_files
                files_to_add = set()
                
                # Add modified tracked files
                for f in modified_files:
                    files_to_add.add(str(f))
                
                # Add untracked files
                files_to_add.update(untracked_files)
                
                if not files_to_add:
                    print("No changes to add")
                    return True
                
                files = list(files_to_add)
                print(f"Staging {len(files)} files...")
        elif not files:
            print("No files specified. Use 'fox add <files>' or 'fox add --all' to add files")
            return False

        added_count = 0
        for file_pattern in files:
            # pathlib glob doesn't support absolute patterns on Windows (e.g. "\MARET\blank.py")
            # Normalize absolute paths into repo-relative paths when possible, and fall back gracefully.
            normalized_pattern = file_pattern
            try:
                # Convert absolute filesystem paths to relative-to-cwd if they're inside the repo.
                p = Path(file_pattern)
                if p.is_absolute():
                    try:
                        normalized_pattern = str(p.resolve().relative_to(Path(".").resolve()))
                    except Exception:
                        normalized_pattern = str(p)
                else:
                    # Handle Windows-style "\foo\bar" which Path treats as absolute-ish on Windows.
                    if isinstance(file_pattern, str) and (file_pattern.startswith("\\") or file_pattern.startswith("/")):
                        normalized_pattern = file_pattern.lstrip("\\/") or file_pattern
            except Exception:
                normalized_pattern = file_pattern

            try:
                file_paths = list(Path(".").glob(normalized_pattern))
            except NotImplementedError:
                file_paths = []
            if not file_paths:
                file_paths = [Path(normalized_pattern)]
            
            for filepath in file_paths:
                if filepath.exists() and filepath.is_file():
                    # Skip files inside the .fox metadata directory. Test path
                    # *components*, not a substring of the whole path: ".fox" in
                    # str(path) also matched ".foxignore", so the one file the
                    # server relies on for ignore rules could never be committed.
                    if ".fox" in filepath.parts:
                        continue
                    
                    # For individual files, always add them (don't check if modified)
                    # For --all, we've already filtered to only modified/untracked files
                    if not add_all and not ("." in files):
                        # Individual file add - always add regardless of modification status
                        pass
                    else:
                        # This is from --all or . command, files are pre-filtered
                        pass
                    
                    file_hash = self.get_file_hash(filepath)
                    
                    # Read file content
                    with open(filepath, "rb") as f:
                        content = f.read()
                    
                    # Store file with compression using subdirectory structure
                    self.store_object_compressed(content, file_hash)
                    
                    # Update delta cache for future delta compression
                    self.update_delta_cache(str(filepath), file_hash)
                    
                    # Add to staging. The entry is named from the file's whole
                    # relative path, not its basename: keying by basename meant
                    # client/fox.py, release/fox.py and client/client1/fox.py all
                    # wrote to the same staging entry and silently overwrote each
                    # other, so only the last one survived the commit. On this
                    # repository that quietly dropped 33 files, including six of
                    # the seven __init__.py files. Hashed rather than sanitised so
                    # a deep path cannot exceed the filesystem's name length.
                    staging_key = hashlib.sha1(
                        str(filepath).replace("\\", "/").encode("utf-8")
                    ).hexdigest()[:16]
                    staging_file = self.staging_dir / f"{staging_key}.json"
                    with open(staging_file, "w") as f:
                        json.dump({
                            "path": str(filepath),
                            "hash": file_hash,
                            "added_at": datetime.now().isoformat()
                        }, f)
                    
                    print(f"Added {filepath}")
                    added_count += 1
                else:
                    print(f"File not found: {filepath}")
        
        if added_count == 0 and (add_all or (files and "." in files)):
            print("No changes to add")
        
        return True
    
    def commit(self, message, parents=None):
        """Commit staged changes"""
        if not self.check_repository("commit"):
            return False
        
        config = self.load_config()
        
        # Check if there are staged files
        staged_files = list(self.staging_dir.glob("*"))
        if not staged_files:
            print("No changes staged for commit")
            return False
        
        # Generate commit ID
        commit_id = hashlib.sha256(f"{message}{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        
        # Get parent commit(s)
        parent = self.get_head_commit_id()
        parents_list = parents if parents else ([parent] if parent else [])
        
        # Collect staged files (path -> {hash, content})
        staged_files_data = {}
        for staging_file in staged_files:
            with open(staging_file, "r") as f:
                file_info = json.load(f)
            
            file_hash = file_info["hash"]
            file_path = file_info["path"]
            
            # Try to load compressed file content
            content = self.load_object_compressed(file_hash)
            
            if content is not None:  # Changed: allow empty content (b'')
                # Encode for storage/transmission
                content_encoded = base64.b64encode(content).decode()
            else:
                # Fallback 1: try reading from old flat structure
                old_path = self.objects_dir / file_hash
                if old_path.exists():
                    with open(old_path, "rb") as f:
                        content_encoded = base64.b64encode(f.read()).decode()
                # Fallback 2: try reading directly from source file if it still exists
                elif Path(file_path).exists():
                    print(f"Warning: Object {file_hash} not found in storage, reading from source file")
                    with open(file_path, "rb") as f:
                        content = f.read()
                        # Store it properly for next time
                        self.store_object_compressed(content, file_hash)
                        content_encoded = base64.b64encode(content).decode()
                # Fallback 3: Handle empty files (hash of empty content)
                elif file_hash.startswith("e3b0c44298fc1c14"):
                    # This is the hash of an empty file - use empty content
                    content_encoded = base64.b64encode(b"").decode()
                else:
                    print(f"Error: Could not find object {file_hash} or source file {file_path}")
                    print(f"Skipping this file from commit")
                    continue
            
            staged_files_data[file_info["path"]] = {
                "hash": file_info["hash"],
                "content": content_encoded
            }

        # Load existing commits for merge
        commits = []
        if self.commits_file.exists():
            with open(self.commits_file, "r") as f:
                commits = json.load(f)

        # Merge last commit snapshot with staged updates/additions
        merged_files_by_path = {}
        if commits:
            last_commit_files = commits[-1].get("files", {})
            for file_hash, file_data in last_commit_files.items():
                merged_files_by_path[file_data["path"]] = {
                    "hash": file_hash,
                    "content": file_data["content"]
                }

        # Staged files replace or add entries
        merged_files_by_path.update(staged_files_data)

        # Build commit file map (hash -> {path, content})
        files = {}
        for file_path, file_data in merged_files_by_path.items():
            files[file_data["hash"]] = {
                "path": file_path,
                "content": file_data["content"]
            }
        
        # Create commit object
        commit = {
            "id": commit_id,
            "message": message,
            "author": config["username"],
            "timestamp": datetime.now().isoformat(),
            "parent": parent,
            "parents": parents_list,
            "files": files
        }
        
        # Save commit locally
        commits.append(commit)
        with open(self.commits_file, "w") as f:
            json.dump(commits, f, indent=2)
        
        # Update HEAD
        self.update_head_commit(commit_id)
        
        # Update index with committed files for fast tracking
        self.update_index_from_commit(files)
        
        # Clear staging area
        shutil.rmtree(self.staging_dir)
        self.staging_dir.mkdir()
        
        # Run garbage collection if we have enough objects
        loose_count = sum(1 for _ in self.objects_dir.rglob("*") if _.is_file())
        if loose_count >= self.pack_threshold:
            print("Running garbage collection...")
            self.pack_objects()
        
        print(f"Committed changes: {commit_id}")
        print(f"Message: {message}")
        print(f"Files changed: {len(staged_files_data)}")
        print(f"Total files in snapshot: {len(files)}")
        
        return True
    
    def create_remote_repository(self, config):
        """Create repository on remote server"""
        try:
            if not self.get_auth_headers():
                print("Authentication required for repository creation.")
                if not self.login():
                    return None

            self._sync_server_url(config)
            response = self._authed_post(
                f"{self._resolve_server_url()}/api/repository/create",
                json={
                    "username": config["username"],
                    "repo_name": (config.get("repo_name") or "").strip(),
                },
                timeout=60,
            )
            
            if response.status_code == 200:
                data = response.json()
                if data["success"]:
                    # Check if repository creation is pending approval
                    if data.get("status") == "pending_approval":
                        team_lead = data.get("team_lead", "team lead")
                        print(f"📝 Repository creation - Pending approval from {team_lead}")
                        if data.get("message"):
                            print(f"   {data['message']}")
                        # Store team_lead for future checks
                        return {"status": "pending", "team_lead": team_lead}
                    
                    # Repository created or already exists
                    repo_id = data.get("repo_id") or data.get("id")
                    if not repo_id:
                        print("Error: Server did not return repository ID")
                        return None
                    
                    # Show message if repository already existed or was just created
                    message = data.get("message")
                    if message and "already exists" in message.lower():
                        print(f"✓ Repository found: {repo_id}")
                    elif message and "approved" in message.lower():
                        print(f"✓ Repository approved and created: {repo_id}")
                    elif message:
                        print(f"ℹ️  {message}")
                    
                    return repo_id
                else:
                    error_msg = data.get('error', '')
                    if "already exists" in error_msg.lower():
                        # Repository exists, try to get its ID and pull
                        print("Repository already exists on server. Attempting to pull existing repository...")
                        repo_id = self.get_existing_repository_id(config)
                        if repo_id:
                            # Set the repo_id and attempt pull
                            config["repo_id"] = repo_id
                            self.save_config(config)
                            if self.pull_existing_repository(config):
                                return repo_id
                        return None
                    else:
                        print(f"Server error: {error_msg}")
            elif response.status_code == 400:
                # Handle FastAPI error format
                try:
                    data = response.json()
                    error_msg = data.get('detail', '')
                    if "already exists" in error_msg.lower():
                        # Repository exists, try to get its ID and pull
                        print("Repository already exists on server. Attempting to pull existing repository...")
                        repo_id = self.get_existing_repository_id(config)
                        if repo_id:
                            # Set the repo_id and attempt pull
                            config["repo_id"] = repo_id
                            self.save_config(config)
                            if self.pull_existing_repository(config):
                                return repo_id
                        return None
                    else:
                        print(f"Server error: {error_msg}")
                except:
                    print(f"HTTP error: {response.status_code}")
            elif response.status_code == 403:
                # Handle permission denied
                try:
                    data = response.json()
                    error_msg = data.get('detail', 'Permission denied')
                    print(f"\n❌ Access Denied: {error_msg}")
                    print(f"\n💡 Your username '{config['username']}' is not registered in the system.")
                    print(f"   Please contact an administrator to create your account.")
                    print(f"\n   To change your username, run: fox --update username")
                except:
                    print(f"\n❌ Access Denied (HTTP 403)")
                    print(f"   Your account may not exist or you don't have permission.")
                    print(f"   To change your username, run: fox --update username")
            elif response.status_code == 401:
                # Unauthorized - likely missing/invalid token
                try:
                    data = response.json()
                    detail = data.get('detail') or data.get('error') or response.text
                    print(f"\n❌ Authentication required: {detail}")
                except Exception:
                    print("\n❌ Authentication required (HTTP 401)")
                print("\nPlease authenticate with the server first:")
                print("  fox login")
                print("Or save a valid access token to your global credentials file:")
                print(f"  {self.global_config_file} \nExample: {json.dumps({'username': config.get('username'), 'access_token': '<TOKEN>'}, indent=2)}")
                return None
            else:
                print(f"HTTP error: {response.status_code}")
        
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
        
        return None
    
    def get_existing_repository_id(self, config):
        """Get the ID of an existing repository on the server"""
        try:
            self._sync_server_url(config)
            server_url = self._resolve_server_url()
            target_names = set(self._repo_name_variants(config.get("repo_name")))

            for name in self._repo_name_variants(config.get("repo_name")):
                response = self._authed_get(
                    f"{server_url}/api/repository/list",
                    params={
                        "username": config["username"],
                        "repo_name": name,
                    },
                    timeout=60,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data["success"] and data.get("repositories"):
                        for repo in data["repositories"]:
                            if repo["name"] in target_names or repo["name"].strip() in target_names:
                                return repo["id"]

            fallback = self._authed_get(
                f"{server_url}/api/repositories/all",
                timeout=60,
            )
            if fallback.status_code == 200:
                payload = fallback.json()
                repos = payload.get("repositories", []) if isinstance(payload, dict) else []
                matches = [r for r in repos if r.get("name") in target_names or (r.get("name") or "").strip() in target_names]
                if len(matches) == 1:
                    return matches[0].get("id")
                if len(matches) > 1:
                    for repo in matches:
                        if repo.get("owner") == config.get("username"):
                            return repo.get("id")
                    print(f"Multiple repositories named '{config.get('repo_name')}' found. Please use a unique repository name.")
            
        except requests.exceptions.RequestException as e:
            print(f"Network error while getting repository ID: {e}")
        
        return None
    
    def pull_existing_repository(self, config):
        """Pull all commits from an existing repository"""
        try:
            response = self._authed_get(
                f"{self._resolve_server_url()}/api/repository/{config['repo_id']}/commits",
                params={"full": "true"},
                timeout=30,
            )
            
            if response.status_code == 200:
                data = response.json()
                if data["success"]:
                    remote_commits = data.get("commits", [])
                    if remote_commits:
                        print(f"Pulling {len(remote_commits)} commits from remote repository...")
                        
                        # Reverse the order since server returns most recent first
                        remote_commits = list(reversed(remote_commits))
                        
                        # Save remote commits to local
                        with open(self.commits_file, "w") as f:
                            json.dump(remote_commits, f, indent=2)
                        
                        # Extract files from the latest commit
                        latest_commit = remote_commits[-1]
                        self.extract_commit_files(latest_commit)
                        
                        # Update HEAD to latest commit
                        with open(self.head_file, "w") as f:
                            f.write(latest_commit["id"])
                        
                        # Initialize index from the latest commit
                        self.init_index_from_last_commit()
                        
                        print("Successfully pulled remote repository")
                        return True
                    else:
                        print("Remote repository is empty")
                        return True
                else:
                    print(f"Failed to pull: {data.get('error')}")
            else:
                print(f"HTTP error while pulling: {response.status_code}")
        
        except requests.exceptions.RequestException as e:
            print(f"Network error while pulling: {e}")
        
        return False
    
    def extract_commit_files(self, commit):
        """Extract files from a commit to the working directory"""
        for file_hash, file_info in commit["files"].items():
            # Handle both old format (dict with path and content) and new format (content string)
            if isinstance(file_info, dict):
                file_path = Path(file_info["path"])
                content = file_info["content"]
            else:
                # If file_info is just a string, we need to get the path from somewhere else
                # For now, let's try to get it from the original commit structure
                print(f"Warning: file_info is a string for hash {file_hash}: {file_info[:100]}...")
                # Try to find path info or skip this file
                continue
            
            # Create directory if needed
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write file content
            try:
                # Try to decode as base64 first (for binary files)
                try:
                    import base64
                    content_bytes = base64.b64decode(content)
                    with open(file_path, "wb") as f:
                        f.write(content_bytes)
                except:
                    # If base64 decode fails, treat as text
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                        
                print(f"Extracted: {file_path}")
            except Exception as e:
                print(f"Failed to extract {file_path}: {e}")

    def get_local_branch_head_commit_id(self, branch_name: str) -> Optional[str]:
        """Return commit id stored in .fox/refs/heads/<branch_name>, or None if missing."""
        if not branch_name or not str(branch_name).strip():
            return None
        self.ensure_refs()
        ref_path = self.heads_dir / str(branch_name).strip().replace("\\", "/")
        if not ref_path.exists():
            return None
        return self._read_ref(ref_path)

    def _parse_push_refspec_args(
        self,
        refspec: Optional[str],
        push_from_branch: Optional[str],
        push_to_branch: Optional[str],
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Resolve Git-style push refspec (local SRC → remote DST).

        Returns (error_message, source_local_branch_or_None, dest_remote_branch_or_None).
        When both branch returns are None, caller should use current HEAD and current branch name.
        """
        has_flags = bool((push_from_branch or "").strip()) or bool((push_to_branch or "").strip())
        rs = (refspec or "").strip()

        if has_flags and rs:
            return "Use either SRC:DST or --from-branch/--to-branch together, not both.", None, None

        if has_flags:
            src = (push_from_branch or "").strip()
            dst = (push_to_branch or "").strip()
            if not src or not dst:
                return (
                    "Provide both --from-branch and --to-branch (example: "
                    "fox push --from-branch feature/login --to-branch main).",
                    None,
                    None,
                )
            return None, src, dst

        if rs:
            if ":" not in rs:
                return (
                    "Invalid refspec: expected SRC:DST (same idea as `git push origin SRC:DST`). "
                    "Example: fox push feature/login:main",
                    None,
                    None,
                )
            src, dst = rs.split(":", 1)
            src, dst = src.strip(), dst.strip()
            if not src or not dst:
                return "Invalid refspec: SRC and DST must both be non-empty.", None, None
            return None, src, dst

        return None, None, None

    def push(
        self,
        archive=False,
        refspec: Optional[str] = None,
        push_from_branch: Optional[str] = None,
        push_to_branch: Optional[str] = None,
    ):
        """Push commits to remote repository.

        Default: push commits reachable from the current branch HEAD to the same branch on the server.

        Refspec (Git-style): ``fox push SRC:DST`` pushes commits reachable from **local** branch ``SRC``
        to the **server** branch ``DST`` without checking out ``SRC``. Current ``HEAD`` is unchanged.
        Alternative: ``fox push --from-branch SRC --to-branch DST`` (for branch names that contain ``:``).
        """
        if not self.check_repository("push"):
            return False

        err, src_branch, dst_branch = self._parse_push_refspec_args(
            refspec, push_from_branch, push_to_branch
        )
        if err:
            print(err)
            return False

        remote_branch_name = dst_branch if dst_branch else (self.get_current_branch() or "main")

        config = self.load_config()
        
        # Check if origin is set
        origin_url = self.get_origin_url()
        if not origin_url:
            print("Fatal: No origin set. Use 'fox set origin <ip:port>' to set the remote repository URL")
            print("  You can also set it globally: fox set origin <ip:port> --global")
            print("Example: fox set origin 192.168.15.207:502 --global")
            return False
        
        # Update server_url to use origin
        self.server_url = origin_url
        config["server_url"] = origin_url
        
        # Link or create remote repository
        if not config.get("repo_id"):
            linked = self.link_remote_repository(config)
            if linked:
                config = self.load_config()
            else:
                print("Creating remote repository...")
                result = self.create_remote_repository(config)

                if isinstance(result, dict) and result.get("status") == "pending":
                    team_lead = result.get("team_lead", "team lead")
                    linked = self.link_remote_repository(config)
                    if linked:
                        config = self.load_config()
                        print("\n✓ Repository has been approved and linked!")
                    else:
                        print(f"\n⏳ Cannot push until repository is approved by {team_lead}.")
                        print("   After approval, run: fox link   then   fox push")
                        print("   Or set manually: fox set repo-id <id-from-web-ui>")
                        return False
                elif not result:
                    print("Failed to create remote repository")
                    return False
                else:
                    config["repo_id"] = result
                    self.save_config(config)
                    print(f"Created remote repository: {result}")

        if not config.get("repo_id"):
            print("❌ Remote repository is not linked. Run: fox login  then  fox link  or  fox push")
            return False

        if src_branch or dst_branch:
            label_src = src_branch or (self.get_current_branch() or "HEAD")
            print(
                f"Refspec push: local branch '{label_src}' → remote branch "
                f"'{remote_branch_name}' (current checkout unchanged)."
            )

        # Load local commits
        if not self.commits_file.exists():
            print("Everything up-to-date")
            return True
        
        with open(self.commits_file, "r") as f:
            commits = json.load(f)
        
        if not commits:
            print("Everything up-to-date")
            return True

        if src_branch:
            head_commit_id = self.get_local_branch_head_commit_id(src_branch)
            if not head_commit_id:
                print(f"Local branch not found or empty ref: {src_branch!r}")
                print("Hint: list branches with  fox branch")
                return False
        else:
            head_commit_id = self.get_head_commit_id()

        # Only consider commits reachable from the chosen local tip (avoid pushing unrelated history)
        commits_by_id = {c.get("id"): c for c in commits if c.get("id")}
        reachable_commits = []
        current_id = head_commit_id
        while current_id:
            commit = commits_by_id.get(current_id)
            if not commit:
                break
            reachable_commits.append(commit)
            parents = self._get_commit_parents(commit)
            current_id = parents[0] if parents else None
        reachable_commits.reverse()  # push oldest → newest

        # Check if there are actually new commits to push by comparing with remote.
        commits_to_push = reachable_commits  # Default: push reachable chain
        try:
            # Include auth header if available
            headers = self.get_auth_headers()

            commits_url = f"{config['server_url']}/api/repository/{config['repo_id']}/commits"

            # Compare against the remote branch we are updating (default: current branch).
            response = self._authed_get(
                commits_url,
                params={"branch": remote_branch_name},
                timeout=60,
            )
            # If server denies branch-specific commit listing (common for issue-scoped users),
            # fall back to default-branch history so we can still detect "already on remote" base commits.
            if response.status_code == 403:
                response = self._authed_get(commits_url, params=None, timeout=60)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    remote_commits = data.get("commits", [])
                    remote_commit_ids = {c["id"] for c in remote_commits}

                    # If a commit is now present on the remote, treat it as merged locally.
                    # This prevents `fox push` from repeatedly treating old pending commits as "submitted".
                    did_sync_remote_status = False
                    try:
                        for c in commits_to_push:
                            cid = c.get("id")
                            if cid and cid in remote_commit_ids and c.get("remote_status") != "merged":
                                c["remote_status"] = "merged"
                                did_sync_remote_status = True
                    except Exception:
                        pass
                    
                    # Filter to only new commits
                    commits_to_push = [c for c in commits_to_push if c["id"] not in remote_commit_ids]

                    # Filter out commits that were already submitted for review (pending approval).
                    commits_to_push = [c for c in commits_to_push if c.get("remote_status") != "pending_approval"]
                    
                    if not commits_to_push:
                        if did_sync_remote_status:
                            try:
                                with open(self.commits_file, "w") as f:
                                    json.dump(commits, f, indent=2)
                            except Exception:
                                pass
                        print("All commits pushed successfully!")
                        return True
                    
                    print(f"Pushing {len(commits_to_push)} new commit(s)...")
        except Exception as e:
            # If we can't check remote, proceed with push attempt
            print(f"Warning: Could not check remote commits: {e}")
            pass
        
        # Verify commit authors match current username (from global config)
        global_config = self.load_global_config()
        current_username = global_config.get('username') or config.get('username')
        
        if current_username:
            for commit in commits_to_push:
                commit_author = commit.get('author')
                if commit_author != current_username:
                    print(f"\n❌ Error: Commit author mismatch detected!")
                    print(f"   Commit {commit['id'][:12]} was created by: {commit_author}")
                    print(f"   Your current username is: {current_username}")
                    print(f"\n💡 This commit was created with a different username.")
                    print(f"   You cannot push commits created by another user.")
                    print(f"\n   Options:")
                    print(f"   1. Switch back to '{commit_author}': fox --update username")
                    print(f"   2. Create a new commit with your current username")
                    return False

        # Always skip commits already marked as pending approval locally.
        # (Even if the remote /commits check failed, we must not re-submit the same pending commit.)
        commits_to_push = [c for c in commits_to_push if c.get("remote_status") != "pending_approval"]
        if not commits_to_push:
            print("All commits pushed successfully!")
            return True
        
        # Push each new commit
        pushed_count = 0
        did_mark_any = False
        for commit in commits_to_push:
            try:
                # Prepare commit data for server
                commit_data = commit.copy()
                
                # Convert file format for server
                # Server expects {file_path: content} format
                server_files = {}
                for file_hash, file_info in commit["files"].items():
                    # Handle both old format (dict with path and content) and new format (content string)
                    if isinstance(file_info, dict):
                        # Use the actual file path as key, not the hash
                        file_path = file_info.get("path", file_hash)
                        server_files[file_path] = file_info["content"]
                    else:
                        # If file_info is just a string, it's already the content
                        # In this case, we don't have the path, so use hash (legacy fallback)
                        server_files[file_hash] = file_info
                
                # Drop ignored paths before they go over the wire. The server enforces the
                # same rules, but doing it here saves uploading what would be discarded.
                matcher = load_ignore_matcher(".")
                skipped_locally = []
                for path in list(server_files):
                    if path == IGNORE_FILENAME:
                        continue
                    pattern = matcher.matched_rule(path)
                    if pattern:
                        skipped_locally.append((path, pattern))
                        server_files.pop(path)
                if skipped_locally:
                    print(f"  Skipping {len(skipped_locally)} ignored file(s):")
                    for path, pattern in skipped_locally[:5]:
                        print(f"    - {path}  ({IGNORE_FILENAME}: {pattern})")
                    if len(skipped_locally) > 5:
                        print(f"    ... and {len(skipped_locally) - 5} more")

                commit_data["files"] = server_files

                # Calculate dynamic timeout based on payload size
                # Minimum 120 seconds, plus 30 seconds per MB of data
                import json as json_module
                payload = {"commit": commit_data, "archive": archive, "branch": remote_branch_name}
                payload_size = len(json_module.dumps(payload))
                dynamic_timeout = max(120, 30 + (payload_size // (1024 * 1024)) * 30)
                
                response = self._authed_post(
                    f"{config['server_url']}/api/repository/{config['repo_id']}/push",
                    json=payload,
                    timeout=dynamic_timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data["success"]:
                        # Check if commit is pending approval
                        if data.get("status") == "pending_approval":
                            reviewer = data.get("team_lead") or data.get("reviewer") or data.get("reviewer_hint") or "team lead/admin"
                            print(f"📝 Commit {commit['id'][:12]} - Pending approval by {reviewer}")
                            if data.get("message"):
                                print(f"   {data['message']}")
                            # Mark as submitted so repeated `fox push` doesn't spam re-submissions.
                            commit["remote_status"] = "pending_approval"
                            commit["submitted_at"] = datetime.now().isoformat()
                        else:
                            print(f"✓ Pushed commit: {commit['id'][:12]}")
                            commit["remote_status"] = "merged"
                            commit["submitted_at"] = datetime.now().isoformat()
                        pushed_count += 1
                        did_mark_any = True
                    else:
                        print(f"Failed to push commit {commit['id']}: {data.get('error')}")
                        return False
                elif response.status_code == 413:
                    # Server refused the payload: a file, or the commit as a whole, is
                    # over the configured limit. Refusing beats silently dropping it.
                    try:
                        detail = response.json().get("detail", "Payload too large")
                    except Exception:
                        detail = "Payload too large"
                    print(f"\nPush rejected: {detail}")
                    print(f"\nAdd the offending paths to {IGNORE_FILENAME} and commit again,")
                    print("or ask an administrator to raise the server limit.")
                    return False
                elif response.status_code == 400:
                    # Handle specific error messages from server
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("detail", "Bad request")

                        if "archived" in error_msg.lower():
                            print(f"\nError: Repository is archived.")
                            print(f"To push to an archived repository, use: fox push --archive")
                            return False
                        else:
                            print(f"\nError: {error_msg}")
                            return False
                    except Exception as parse_error:
                        print(f"\nError: Bad request (status code 400)")
                        print(f"Response: {response.text if hasattr(response, 'text') else 'No details'}")
                        return False
                else:
                    print(f"\nError: HTTP {response.status_code}")
                    try:
                        error_data = response.json()
                        if "detail" in error_data:
                            print(f"Details: {error_data['detail']}")
                    except:
                        pass
                    return False
            
            except requests.exceptions.RequestException as e:
                print(f"Network error while pushing {commit['id']}: {e}")
                return False
        
        if pushed_count == 0:
            print("All commits pushed successfully!")
        elif archive:
            print("All commits pushed successfully and repository archived!")
        else:
            print("All commits pushed successfully!")

        # Persist updated commit metadata (remote_status/submitted_at) so later pushes skip resubmitting.
        if did_mark_any:
            try:
                with open(self.commits_file, "w") as f:
                    json.dump(commits, f, indent=2)
            except Exception:
                pass
        return True
    
    def pull(self):
        """Pull commits from remote repository"""
        if not self.check_repository("pull"):
            return False
        
        config = self.load_config()
        
        # Check if origin is set
        origin_url = self.get_origin_url()
        if not origin_url:
            print("Fatal: No origin set. Use 'fox set origin <ip:port>' to set the remote repository URL")
            print("Example: fox set origin 192.168.15.207:502")
            return False
        
        # Update server_url to use origin
        self.server_url = origin_url
        config["server_url"] = origin_url
        self.save_config(config)
        
        if not config.get("repo_id"):
            # Fresh machine case: resolve existing remote repo by username/repo_name
            resolved_repo_id = self.get_existing_repository_id(config)
            if resolved_repo_id:
                config["repo_id"] = resolved_repo_id
                self.save_config(config)
                print(f"Linked to existing remote repository: {resolved_repo_id}")
            else:
                print("No remote repository configured")
                print("Tip: Ensure repo name/username match an existing server repository, or run 'fox push' to create one.")
                return False
        
        try:
            # Get current HEAD
            since_commit = None
            if self.head_file.exists():
                with open(self.head_file, "r") as f:
                    since_commit = f.read().strip()
            
            # Pull from server
            current_branch = self.get_current_branch() or "main"
            params = {"since": since_commit, "branch": current_branch} if since_commit else {"branch": current_branch}
            response = self._authed_get(
                f"{config['server_url']}/api/repository/{config['repo_id']}/pull",
                params=params,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                if data["success"]:
                    commits = data["commits"]

                    # Sync branch refs from server so branch list/checkout work on fresh machines
                    try:
                        branches_response = self._authed_get(
                            f"{config['server_url']}/api/repository/{config['repo_id']}/branches",
                            timeout=30
                        )
                        if branches_response.status_code == 200:
                            branches_data = branches_response.json()
                            if branches_data.get("success"):
                                self.ensure_refs()
                                for branch in branches_data.get("branches", []):
                                    branch_name = branch.get("name")
                                    branch_head = branch.get("head")
                                    if branch_name:
                                        self._write_ref(self.heads_dir / branch_name, branch_head or "")
                    except Exception:
                        pass
                    
                    if not commits:
                        print("Already up to date")
                        return True
                    
                    # Update local repository with pulled commits
                    for commit in commits:
                        normalized_files = {}
                        # Save files from commit with compression
                        for file_hash, file_info in commit.get("files", {}).items():
                            if isinstance(file_info, dict):
                                content_b64 = file_info.get("content", "")
                                file_path = file_info.get("path")
                            else:
                                content_b64 = file_info
                                file_path = None

                            # Decode base64 content
                            try:
                                decoded_content = base64.b64decode(content_b64)
                            except Exception:
                                print(f"Failed to decode content for {file_hash}, skipping")
                                continue

                            # Store with compression using new structure
                            self.store_object_compressed(decoded_content, file_hash)

                            # Preserve path/content structure for later extraction
                            normalized_files[file_hash] = {
                                "path": file_path if file_path else file_hash,
                                "content": content_b64
                            }

                        commit["files"] = normalized_files
                    
                    # Update local commits
                    local_commits = []
                    if self.commits_file.exists():
                        with open(self.commits_file, "r") as f:
                            local_commits = json.load(f)
                    
                    local_commits.extend(commits)
                    with open(self.commits_file, "w") as f:
                        json.dump(local_commits, f, indent=2)
                    
                    # Update HEAD
                    if data.get("head"):
                        self.update_head_commit(data["head"])

                    # Restore files for the latest pulled commit into the working directory
                    latest_commit = commits[-1]
                    self.extract_commit_files(latest_commit)
                    self.update_index_from_commit(latest_commit.get("files", {}))
                    
                    print(f"Pulled {len(commits)} commits")
                    return True
                else:
                    print(f"Server error: {data.get('error')}")
            else:
                print(f"HTTP error: {response.status_code}")
        
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
        
        return False
    
    def status(self):
        """Show repository status with colored output"""
        if not self.check_repository("status"):
            return False
        
        config = self.load_config()
        origin_url = self.get_origin_url()
        
        # ANSI color codes
        RED = '\033[31m'
        GREEN = '\033[32m'
        YELLOW = '\033[33m'
        RESET = '\033[0m'
        
        print(f"Repository: {config['username']}/{config['repo_name']}")
        print(f"Origin: {origin_url if origin_url else 'Not set'}")
        
        # Show current HEAD
        current_branch = self.get_current_branch()
        head = self.get_head_commit_id()
        if head:
            branch_display = f" ({current_branch})" if current_branch else ""
            print(f"Current commit: {head}{branch_display}")
        else:
            print("No commits yet")
        
        # Get staged files
        staged_files = {}
        staged_file_paths = set()
        
        # Collect staged files
        for staging_file in self.staging_dir.glob("*"):
            with open(staging_file, "r") as f:
                file_info = json.load(f)
                file_path = file_info['path']
                staged_files[file_path] = file_info
                staged_file_paths.add(file_path)
        
        # Get all files in working directory
        all_files = self.get_all_files()
        all_file_paths = {str(f) for f in all_files}
        
        # Get tracked files (from index or last commit)
        index = self.load_index()
        if not index:
            self.init_index_from_last_commit()
            index = self.load_index()
        
        tracked_files = set(index.keys()) if index else set()
        
        # Get modified tracked files
        modified_files = self.get_modified_files_comprehensive()
        modified_file_paths = {str(f) for f in modified_files}
        
        # Categorize files
        unstaged_files = modified_file_paths - staged_file_paths
        untracked_files = all_file_paths - tracked_files - staged_file_paths
        
        # Show status
        has_changes = len(staged_files) > 0 or len(unstaged_files) > 0 or len(untracked_files) > 0
        
        if not has_changes:
            print("\nNothing to commit, working tree clean")
            return True
        
        print()  # Empty line for spacing
        
        if staged_files:
            print("Changes to be committed:")
            print("  (use \"fox reset <file>...\" to unstage)")
            print()
            for file_path in sorted(staged_files.keys()):
                print(f"  {GREEN}modified:   {file_path}{RESET}")
        
        if unstaged_files:
            if staged_files:
                print()  # Add spacing between sections
            print("Changes not staged for commit:")
            print("  (use \"fox add <file>...\" to update what will be committed)")
            print('  (use "fox checkout-files --from <branch|commit> <path>..." to restore paths without switching branches)')
            print()
            for file_path in sorted(unstaged_files):
                print(f"  {RED}modified:   {file_path}{RESET}")
        
        if untracked_files:
            if staged_files or unstaged_files:
                print()  # Add spacing between sections
            print("Untracked files:")
            print("  (use \"fox add <file>...\" to include in what will be committed)")
            print()
            for file_path in sorted(untracked_files):
                print(f"  {YELLOW}{file_path}{RESET}")
        
        return True
    
    def status_short(self):
        """Show short repository status"""
        if not self.check_repository("status"):
            return False
        
        # Show staged files with short format
        staged_files = list(self.staging_dir.glob("*"))
        if staged_files:
            for staging_file in staged_files:
                with open(staging_file, "r") as f:
                    file_info = json.load(f)
                print(f"A  {file_info['path']}")
        else:
            print("Nothing staged")
        
        return True
    
    def _get_server_commits_for_log(self):
        """Fetch commits from server for log display. Returns (commits_by_id, head_id) or (None, None) on failure."""
        try:
            config = self.load_config()
            if not config:
                return None, None
            server_url = self._resolve_server_url()
            repo_id = config.get('repo_id')
            # If repo_id not cached locally, look it up by name from the public endpoint
            if not repo_id and config.get('repo_name'):
                try:
                    r = self._authed_get(
                        f"{server_url}/api/repositories/all",
                        timeout=10,
                    )
                    if r.status_code == 200:
                        for repo in r.json().get('repositories', []):
                            if repo.get('name') == config['repo_name']:
                                repo_id = repo['id']
                                # Save it so future calls are fast
                                config['repo_id'] = repo_id
                                self.save_config(config)
                                break
                except Exception:
                    pass
            if not repo_id:
                print("(log: repo_id not found - run 'fox push' first to link to server)")
                return None, None
            branch = self.get_current_branch() or 'main'
            resp = self._authed_get(
                f"{server_url}/api/repository/{repo_id}/commits",
                params={'branch': branch},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"(log: server returned HTTP {resp.status_code} for commits - showing local history)")
                return None, None
            data = resp.json()
            if not data.get('success'):
                print(f"(log: server error: {data.get('error', 'unknown')} - showing local history)")
                return None, None
            commits = data.get('commits', [])
            if not commits:
                return {}, None
            commits_by_id = {c['id']: c for c in commits}
            # HEAD is the commit not referenced as a parent by any other commit in the set
            all_parents = set()
            for c in commits:
                for pid in (c.get('parents') or ([c['parent']] if c.get('parent') else [])):
                    if pid:
                        all_parents.add(pid)
            head_candidates = [c['id'] for c in commits if c['id'] not in all_parents]
            head_id = head_candidates[0] if head_candidates else commits[-1]['id']
            return commits_by_id, head_id
        except requests.exceptions.ConnectionError:
            print(f"(log: cannot reach server - showing local history)")
            return None, None
        except Exception as e:
            print(f"(log: unexpected error: {e} - showing local history)")
            return None, None

    def log(self, max_count=None):
        """Show commit history"""
        if not self.check_repository("log"):
            return False

        # Try server first for complete, up-to-date history
        commits_by_id, head_id = self._get_server_commits_for_log()
        if commits_by_id is not None:
            if not commits_by_id:
                print("No commits yet")
                return True
            print("Commit history:")
            current_id = head_id
            shown = 0
            while current_id:
                commit = commits_by_id.get(current_id)
                if not commit:
                    break
                parents = commit.get('parents') or ([commit['parent']] if commit.get('parent') else [])
                parents = [p for p in parents if p]
                print(f"\nCommit: {commit['id']}")
                print(f"Author: {commit['author']}")
                print(f"Date: {commit['timestamp']}")
                print(f"Message: {commit['message']}")
                if parents:
                    print(f"Parents: {', '.join(parents)}")
                print(f"Files: {len(commit.get('files', []))}")
                shown += 1
                if max_count and shown >= max_count:
                    break
                current_id = parents[0] if parents else None
            return True

        # Fallback: read from local commits.json
        commits = self.load_commits()
        if not commits:
            print("No commits yet")
            return True

        print("Commit history:")
        head_commit_id = self.get_head_commit_id()
        current_id = head_commit_id
        shown = 0
        while current_id:
            commit = self.get_commit_by_id(current_id)
            if not commit:
                break
            print(f"\nCommit: {commit['id']}")
            print(f"Author: {commit['author']}")
            print(f"Date: {commit['timestamp']}")
            print(f"Message: {commit['message']}")
            parents = self._get_commit_parents(commit)
            if parents:
                print(f"Parents: {', '.join(parents)}")
            print(f"Files: {len(commit['files'])}")
            shown += 1
            if max_count and shown >= max_count:
                break
            current_id = parents[0] if parents else None

        return True

    def log_oneline(self, max_count=None):
        """Show commit history in one-line format"""
        if not self.check_repository("log"):
            return False

        # Try server first for complete, up-to-date history
        commits_by_id, head_id = self._get_server_commits_for_log()
        if commits_by_id is not None:
            if not commits_by_id:
                print("No commits yet")
                return True
            current_id = head_id
            shown = 0
            while current_id:
                commit = commits_by_id.get(current_id)
                if not commit:
                    break
                parents = commit.get('parents') or ([commit['parent']] if commit.get('parent') else [])
                parents = [p for p in parents if p]
                date = (commit['timestamp'] or '')[:10]
                print(f"{commit['id']} {date} {commit['author']}: {commit['message']}")
                shown += 1
                if max_count and shown >= max_count:
                    break
                current_id = parents[0] if parents else None
            return True

        # Fallback: read from local commits.json
        commits = self.load_commits()
        if not commits:
            print("No commits yet")
            return True

        head_commit_id = self.get_head_commit_id()
        current_id = head_commit_id
        shown = 0
        while current_id:
            commit = self.get_commit_by_id(current_id)
            if not commit:
                break
            date = commit['timestamp'][:10]
            print(f"{commit['id']} {date} {commit['author']}: {commit['message']}")
            shown += 1
            if max_count and shown >= max_count:
                break
            parents = self._get_commit_parents(commit)
            current_id = parents[0] if parents else None

        return True
    
    def gc(self):
        """
        Garbage collection - optimize repository by packing loose objects
        Similar to 'git gc'
        """
        if not self.check_repository("gc"):
            return False
        
        print("Running garbage collection...")
        
        # Count loose objects
        loose_count = sum(1 for _ in self.objects_dir.rglob("*") if _.is_file())
        print(f"Found {loose_count} loose objects")
        
        if loose_count == 0:
            print("Nothing to pack")
            return True
        
        # Pack objects
        self.pack_objects()
        
        # Report savings
        pack_count = sum(1 for _ in self.packs_dir.glob("*.pack"))
        remaining_loose = sum(1 for _ in self.objects_dir.rglob("*") if _.is_file())
        
        print(f"Created {pack_count} pack file(s)")
        print(f"Remaining loose objects: {remaining_loose}")
        print("Repository optimized!")
        
        return True

def print_version():
    """Print FoxNest version information"""
    print("FoxNest VCS")
    print("Version: 1.0.5")
    print("")

def print_extended_help():
    """Print extended help with examples"""
    print_version()
    print("USAGE:")
    print("  fox <command> [options]")
    print("")
    print("COMMANDS:")
    print("  init                     Initialize a new repository")
    print("  add <files>             Add files to staging area")
    print("  add --all               Add all files in working directory")
    print("  add .                   Add all files in working directory")
    print("  commit -m <message>     Commit staged changes")
    print("  set origin <url>        Set remote repository URL")
    print("  set username <name>     Set global username")
    print("  --update username       Update username interactively")
    print("  push [SRC:DST]          Push commits to server (optional Git-style refspec: local SRC → remote DST)")
    print("  push --archive          Push and archive repository")
    print("  link                    Link to approved remote repo (fixes missing repo_id)")
    print("  pull                    Pull commits from server") 
    print("  status                  Show repository status")
    print("  log                     Show commit history")
    print("  branch                  List, create, or delete branches")
    print("  checkout <branch>       Switch branches")
    print("  checkout-files          Restore specific paths from another branch/commit (HEAD unchanged)")
    print("  merge <branch>          Merge a branch into current")
    print("  compare <from> <to>     Diff files between two branches or commits (server)")
    print("  file-history <path>     List file revisions on server (--lines N-M for line slice history)")
    print("  delete-commit <id>      Remove current branch tip (unpushed = local; pushed = server+local)")
    print("  rollback <commit-id>    Roll back current branch to an ancestor commit (same branch)")
    print("  tag                     Manage tags")
    print("  release                 Manage releases (semantic versions)")
    print("  pr                      Manage pull requests")
    print("  config                  Show configuration")
    print("  gc                      Optimize repository (garbage collection)")
    print("  docs [path]             Generate documentation locally")
    print("  generate-docs           Generate documentation on server")
    print("")
    print("  AUTOMATION AND INTEGRATION")
    print("  token                   Named, scoped, revocable access tokens")
    print("  ssh                     Register SSH keys; sign in without a password")
    print("  webhook                 Notify an external service when things happen")
    print("  status-check            Report or view CI results against a commit")
    print("  hook                    Server-side push rules (pre-receive policy)")
    print("  search                  Find repositories, commits or code")
    print("  admin                   Lockouts, schema health and backups (admin)")
    print("")
    print("  help, --help, -h        Show this help message")
    print("  version, --version, -v  Show version information")
    print("")
    print("EXAMPLES:")
    print("  fox set username alice")
    print("  fox init --username alice --repo-name myproject")
    print("  fox set origin 192.168.15.207:502")
    print("  fox add *.py README.md")
    print("  fox add --all")
    print("  fox commit -m 'Initial commit'")
    print("  fox push")
    print("  fox push feature/login:main   # like git push origin feature:main — update server main from local branch")
    print("  fox --update username    # Change your username")
    print("  fox config               # View current settings")
    print("  fox push --archive")
    print("  fox pull")
    print("  fox status")
    print("  fox log --oneline")
    print("  fox branch")
    print("  fox branch create feature/login")
    print("  fox checkout feature/login")
    print("  fox checkout-files --from feature/login README.md src/app.py")
    print("  fox checkout-files --from abc1234 --server README.md   # force server branch by name")
    print("  fox merge feature/login")
    print("  fox compare main feature/login")
    print("  fox compare main feature/login --path src/app.py")
    print("  fox compare abc1234 def5678 --json")
    print("  fox file-history src/app.py --branch main --lines 10-40")
    print("  fox delete-commit abc123 --branch feature   # must be branch tip; main needs manage perms on server")
    print("  fox rollback abc1234def567   # ancestor of current branch tip")
    print("  fox tag create v1.0.0")
    print("  fox release create 1.0.0 --title 'Initial release'")
    print("  fox pr create --title 'Feature' --source feature/login --target main")
    print("  fox gc                  # Optimize repository storage")
    print("  fox docs                # Generate docs for current project")
    print("  fox generate-docs       # Push latest code, then generate docs on server")
    print("  fox token create ci-runner --scope repo:write   # a credential for a build agent")
    print("  fox ssh add work-laptop && fox ssh login        # password-free sign-in")
    print("  fox webhook add https://ci.example/hook --event push")
    print("  fox status-check report ci/unit-tests success   # from inside a CI job")
    print("  fox hook add no-secrets --forbid-content 'AKIA[0-9A-Z]{16}'")
    print("")
    print("GETTING STARTED:")
    print("  1. Set username: fox set username <your_name>")
    print("  2. Initialize repo: fox init")
    print("  3. Set origin: fox set origin <ip:port>")
    print("  4. Add files: fox add <files>")
    print("  5. Commit: fox commit -m \"message\"")
    print("  6. Push: fox push")
    print("")
    print("TROUBLESHOOTING:")
    print("  If you see 'Access Denied' when pushing:")
    print("  - Your username must be registered by an administrator")
    print("  - Update your username: fox --update username")
    print("  - Contact your system administrator to create your account")
    print("")
    print("For more information, visit the documentation or run 'fox <command> --help'")

_PROJECT_DOC_KEY_TO_NAME = {
    "readme": "README.md",
    "api": "API Documentation",
    "architecture": "Architecture",
    "installation": "Installation Guide",
    "database": "Database Schema",
    "requirements": "Requirements (SRS)",
    "use_cases": "Use Cases",
    "release_notes": "Release Notes",
    "user_manual": "User Manual",
    "api_usage": "API Usage Guide",
    "database_er": "Database ER",
    "index": "index.md",
}

_PROJECT_DOC_ALIAS_TO_KEY = {
    "readme": "readme",
    "readme.md": "readme",
    "api": "api",
    "api_documentation": "api",
    "api documentation": "api",
    "architecture": "architecture",
    "installation": "installation",
    "installation_guide": "installation",
    "installation guide": "installation",
    "database": "database",
    "database_schema": "database",
    "database schema": "database",
    "requirements": "requirements",
    "requirements_srs": "requirements",
    "requirements (srs)": "requirements",
    "use_cases": "use_cases",
    "use cases": "use_cases",
    "release_notes": "release_notes",
    "release notes": "release_notes",
    "user_manual": "user_manual",
    "user manual": "user_manual",
    "api_usage": "api_usage",
    "api usage": "api_usage",
    "api_usage_guide": "api_usage",
    "database_er": "database_er",
    "database er": "database_er",
    "index": "index",
    "index.md": "index",
}

def _parse_docs_args(raw_docs):
    if not raw_docs:
        return []
    parsed = []
    for item in raw_docs:
        for token in str(item).split(","):
            cleaned = token.strip().lower().replace("-", "_")
            if cleaned:
                parsed.append(cleaned)
    return parsed

def _normalize_project_doc_keys(raw_docs):
    if not raw_docs:
        return None, []

    keys = []
    invalid = []
    for token in raw_docs:
        if token == "all":
            return None, []
        resolved = _PROJECT_DOC_ALIAS_TO_KEY.get(token)
        if resolved:
            if resolved not in keys:
                keys.append(resolved)
        else:
            invalid.append(token)
    return keys, invalid

def _print_project_doc_options():
    print("\n📚 Available project document names:")
    for key, name in _PROJECT_DOC_KEY_TO_NAME.items():
        print(f"  - {key:<13} -> {name}")

def main():
    # Handle special commands first
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ['help', '--help', '-h']:
            print_extended_help()
            return
        elif cmd in ['version', '--version', '-v']:
            print_version()
            return
    
    parser = argparse.ArgumentParser(
        prog="fox",
        description="🦊 FoxNest Version Control System",
        epilog="Use 'fox help' for detailed help with examples",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Add global options
    parser.add_argument("--version", "-v", action="store_true", help="Show version information")
    parser.add_argument("--update", choices=["username"], help="Update configuration (e.g., --update username)")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands", metavar="<command>")
    
    # Init command
    init_parser = subparsers.add_parser("init", help="Initialize a new repository")
    init_parser.add_argument("--username", help="Username for commits")
    init_parser.add_argument("--repo-name", help="Repository name")
    init_parser.add_argument("--server", help="Server URL (default: http://localhost:33333)")
    
    # Add command  
    add_parser = subparsers.add_parser("add", help="Add files to staging area")
    add_parser.add_argument("files", nargs="*", help="Files to add (supports wildcards)")
    add_parser.add_argument("--all", "-A", action="store_true", help="Add all files in working directory")
    
    # Commit command
    commit_parser = subparsers.add_parser("commit", help="Commit staged changes")
    commit_parser.add_argument("-m", "--message", required=True, help="Commit message")
    commit_parser.add_argument("--author", help="Override commit author")
    
    # Push command
    push_parser = subparsers.add_parser(
        "push",
        help="Push commits to server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Git-style refspec (same idea as git push origin SRC:DST):\n"
            "  fox push SRC:DST\n"
            "    Push commits reachable from local branch SRC to server branch DST.\n"
            "    Does not checkout SRC; current branch and working tree are unchanged.\n"
            "  fox push --from-branch SRC --to-branch DST\n"
            "    Same as SRC:DST when branch names contain ':'.\n"
            "Examples:\n"
            "  fox push feature/login:main\n"
            "  fox push --from-branch feature/login --to-branch main --archive"
        ),
    )
    push_parser.add_argument("--archive", action="store_true", help="Archive repository after push")
    push_parser.add_argument(
        "--from-branch",
        dest="push_from_branch",
        metavar="BRANCH",
        default=None,
        help="With --to-branch: local branch whose history to push (alternative to SRC:DST)",
    )
    push_parser.add_argument(
        "--to-branch",
        dest="push_to_branch",
        metavar="BRANCH",
        default=None,
        help="With --from-branch: server branch to update (alternative to SRC:DST)",
    )
    push_parser.add_argument(
        "refspec",
        nargs="?",
        default=None,
        metavar="SRC:DST",
        help="Refspec: push local SRC to remote DST (see epilog below)",
    )
    
    # Pull command
    pull_parser = subparsers.add_parser("pull", help="Pull commits from server")
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Show repository status")
    status_parser.add_argument("--short", "-s", action="store_true", help="Show short status")
    
    # Log command
    log_parser = subparsers.add_parser("log", help="Show commit history")
    log_parser.add_argument("--oneline", action="store_true", help="Show one line per commit")
    log_parser.add_argument("-n", "--max-count", type=int, help="Limit number of commits shown")

    # Branch command
    branch_parser = subparsers.add_parser("branch", help="List, create, or delete branches")
    branch_subparsers = branch_parser.add_subparsers(dest="branch_command", help="Branch commands")
    branch_subparsers.add_parser("list", help="List branches")
    branch_create = branch_subparsers.add_parser("create", help="Create a branch")
    branch_create.add_argument("name", help="Branch name")
    branch_create.add_argument("--from", dest="from_commit", help="Start branch from commit id")
    branch_create.add_argument(
        "--from-branch",
        dest="from_branch",
        metavar="BRANCH",
        help="Start branch from another branch tip (local ref or server branch name)",
    )
    branch_create.add_argument("--checkout", action="store_true", help="Checkout after creation")
    branch_delete = branch_subparsers.add_parser("delete", help="Delete a branch")
    branch_delete.add_argument("name", help="Branch name")

    # Checkout command
    checkout_parser = subparsers.add_parser("checkout", help="Switch branches")
    checkout_parser.add_argument("branch", help="Branch name")
    checkout_parser.add_argument("-b", "--create", action="store_true", help="Create and switch to new branch")

    checkout_files_parser = subparsers.add_parser(
        "checkout-files",
        help="Restore working-tree files from another branch or commit without switching branches",
    )
    checkout_files_parser.add_argument(
        "--from",
        dest="checkout_files_from",
        required=True,
        metavar="REF",
        help="Local branch name, commit id (or unique prefix), or server branch name when using --server",
    )
    checkout_files_parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Repository-relative file paths to restore",
    )
    checkout_files_parser.add_argument(
        "--server",
        action="store_true",
        dest="checkout_files_server",
        help="Always fetch from server via branch name (GET /file); ignores local commits.json for content",
    )
    checkout_files_parser.add_argument(
        "--server-commit",
        action="store_true",
        dest="checkout_files_server_commit",
        help="Copy files on the server side (creates a commit on the current branch using the server copy-files API)",
    )

    # Merge command
    merge_parser = subparsers.add_parser("merge", help="Merge a branch into current")
    merge_parser.add_argument("branch", help="Source branch")

    # Compare command (server /compare API)
    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare file trees between two branches or commits (server)",
    )
    compare_parser.add_argument(
        "from_ref",
        help="Branch name or commit id for the 'from' (base) side",
    )
    compare_parser.add_argument(
        "to_ref",
        help="Branch name or commit id for the 'to' side",
    )
    compare_parser.add_argument(
        "--path",
        dest="compare_path",
        metavar="PATH",
        help="Limit diff to one path (e.g. README.md)",
    )
    compare_parser.add_argument(
        "--json",
        action="store_true",
        dest="compare_json",
        help="Print raw JSON from the server",
    )

    file_history_parser = subparsers.add_parser(
        "file-history",
        help="Show server-side revision history for a file (optional line-range events)",
    )
    file_history_parser.add_argument("path", help="Repository path, e.g. src/app.py")
    file_history_parser.add_argument("--branch", default=None, help="Limit to commits reachable from this branch")
    file_history_parser.add_argument(
        "--lines",
        dest="file_history_lines",
        metavar="START-END",
        help="Inclusive 1-based line range (e.g. 10-25); lists revisions where that slice changed",
    )
    file_history_parser.add_argument(
        "--json",
        action="store_true",
        dest="file_history_json",
        help="Print raw JSON from the server",
    )

    delete_commit_parser = subparsers.add_parser(
        "delete-commit",
        help="Remove a branch-tip commit (server + local when linked; unpushed = local only)",
    )
    delete_commit_parser.add_argument("commit", help="Commit id or unique local prefix")
    delete_commit_parser.add_argument("--branch", default=None, help="Branch whose tip must match (default: current branch)")
    delete_commit_parser.add_argument(
        "--local-only",
        action="store_true",
        dest="delete_commit_local_only",
        help="Only modify local store; refuses if commit is still the server branch tip",
    )
    delete_commit_parser.add_argument(
        "--expected-head",
        dest="delete_commit_expected_head",
        default=None,
        help="Optional CAS: must equal the commit id being deleted",
    )

    # Rollback command
    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Roll back the current branch to an ancestor commit (same branch; server or offline)",
    )
    rollback_parser.add_argument(
        "commit_id",
        help="Ancestor commit id (or unique prefix). Branch tip moves forward with a rollback commit on the server.",
    )

    # Tag command
    tag_parser = subparsers.add_parser("tag", help="Manage tags")
    tag_subparsers = tag_parser.add_subparsers(dest="tag_command", help="Tag commands")
    tag_subparsers.add_parser("list", help="List tags")
    tag_create = tag_subparsers.add_parser("create", help="Create a tag")
    tag_create.add_argument("name", help="Tag name")
    tag_create.add_argument("--commit", dest="commit_id", help="Commit id")
    tag_create.add_argument("--message", help="Tag message")
    tag_delete = tag_subparsers.add_parser("delete", help="Delete a tag")
    tag_delete.add_argument("name", help="Tag name")

    # Release command
    release_parser = subparsers.add_parser("release", help="Manage releases")
    release_subparsers = release_parser.add_subparsers(dest="release_command", help="Release commands")
    release_subparsers.add_parser("list", help="List releases")
    release_create = release_subparsers.add_parser("create", help="Create a release")
    release_create.add_argument("version", help="Semantic version (e.g., 1.2.3)")
    release_create.add_argument("--tag", help="Tag name (optional)")
    release_create.add_argument("--title", help="Release title")
    release_create.add_argument("--notes", help="Release notes")

    # Pull request command
    pr_parser = subparsers.add_parser("pr", help="Manage pull requests")
    pr_subparsers = pr_parser.add_subparsers(dest="pr_command", help="PR commands")
    pr_subparsers.add_parser("list", help="List pull requests")
    pr_create = pr_subparsers.add_parser("create", help="Create a pull request")
    pr_create.add_argument("--title", required=True, help="PR title")
    pr_create.add_argument("--source", required=True, help="Source branch")
    pr_create.add_argument("--target", required=True, help="Target branch")
    pr_create.add_argument("--description", help="PR description")
    pr_close = pr_subparsers.add_parser("close", help="Close a pull request")
    pr_close.add_argument("id", type=int, help="PR id")
    pr_merge = pr_subparsers.add_parser("merge", help="Merge a pull request")
    pr_merge.add_argument("id", type=int, help="PR id")
    pr_resolve = pr_subparsers.add_parser("resolve", help="Resolve merge conflicts for a PR (download/edit/submit)")
    pr_resolve.add_argument("id", type=int, help="PR id")
    pr_resolve.add_argument("--session", type=int, dest="session_id", help="Merge conflict session id (from merge response)")
    pr_resolve.add_argument("--dir", dest="directory", help="Directory to store conflict files (default: .fox/merge-conflicts/...)")
    pr_resolve.add_argument("--submit", action="store_true", help="Submit *.resolved files and finalize merge")
    
    # Set command (for setting origin and username)
    set_parser = subparsers.add_parser("set", help="Set repository configuration")
    set_subparsers = set_parser.add_subparsers(dest="set_command", help="Set commands")
    origin_parser = set_subparsers.add_parser("origin", help="Set remote origin URL")
    origin_parser.add_argument("url", help="Remote origin URL (e.g., 192.168.15.207:502)")
    origin_parser.add_argument("--global", dest="global_origin", action="store_true", help="Set origin globally for all repositories")
    username_parser = set_subparsers.add_parser("username", help="Set global username")
    username_parser.add_argument("username", help="Your username for commits")
    repo_id_parser = set_subparsers.add_parser("repo-id", help="Link this local repo to a server repo_id")
    repo_id_parser.add_argument("repo_id", help="Repository ID from server (e.g., bb3b2817c768fe6c)")
    
    # Config command (show configuration)
    config_parser = subparsers.add_parser("config", help="Show FoxNest configuration")
    
    # Garbage collection command
    gc_parser = subparsers.add_parser("gc", help="Optimize repository (garbage collection)")
    
    # Documentation commands
    docs_parser = subparsers.add_parser("docs", help="Generate documentation locally using Ollama")
    docs_parser.add_argument("path", nargs="?", default=".", help="Path to project directory (default: current directory)")
    docs_parser.add_argument("--output", "-o", help="Output directory for docs (default: ./docs)")
    docs_parser.add_argument("--model", default=None, help="Ollama model (default: FOXNEST_DOCS_MODEL or qwen2.5-coder:32b)")
    docs_parser.add_argument("--docs", nargs="+", help="Only generate selected docs (comma/space separated): language names and/or dependencies,overview,architecture")
    docs_parser.add_argument("--all-docs", action="store_true", help="Generate all available local docs")
    docs_parser.add_argument("--list-docs", action="store_true", help="List selectable local docs and exit")
    
    generate_docs_parser = subparsers.add_parser("generate-docs", help="Generate project documentation on server using Qwen")
    generate_docs_parser.add_argument("--repo-id", help="Repository ID (optional, auto-detected from local config)")
    generate_docs_parser.add_argument("--model", default=None, help="Ollama model (default: qwen2.5-coder:32b)")
    generate_docs_parser.add_argument("--docs", nargs="+", help="Only generate selected project docs (comma/space separated). Use --list-docs to view names")
    generate_docs_parser.add_argument("--all-docs", action="store_true", help="Generate all project docs")
    generate_docs_parser.add_argument("--list-docs", action="store_true", help="List selectable project docs and exit")
    
    # Help command
    help_parser = subparsers.add_parser("help", help="Show help information")

    # Login command
    login_parser = subparsers.add_parser("login", help="Login to server and save access token to global config")
    login_parser.add_argument("--username", help="Username for login")
    login_parser.add_argument("--password", help="Password for login (not recommended on CLI)")
    # Logout command
    logout_parser = subparsers.add_parser("logout", help="Remove saved access token from global config")

    # Personal access tokens: named, scoped, individually revocable credentials.
    token_parser = subparsers.add_parser("token", help="Manage personal access tokens")
    token_subparsers = token_parser.add_subparsers(dest="token_command", help="Token commands")

    token_create = token_subparsers.add_parser("create", help="Create a new access token")
    token_create.add_argument("name", help="A name you will recognise later, e.g. 'ci-runner'")
    token_create.add_argument(
        "--scope", action="append", dest="scopes",
        choices=["repo:read", "repo:write", "admin"],
        help="Grant a scope (repeatable). Defaults to repo:read.",
    )
    token_create.add_argument("--expires-in-days", type=int, help="Expire the token after N days")
    token_create.add_argument(
        "--use", action="store_true",
        help="Also save the new token as this machine's credential",
    )

    token_list = token_subparsers.add_parser("list", help="List your access tokens")
    token_list.add_argument(
        "--all", action="store_true", dest="include_revoked",
        help="Include revoked and expired tokens",
    )

    token_revoke = token_subparsers.add_parser("revoke", help="Revoke an access token by id")
    token_revoke.add_argument("token_id", help="Token id from 'fox token list'")

    token_use = token_subparsers.add_parser("use", help="Authenticate this machine with an existing token")
    token_use.add_argument("token", help="The fxp_... token value")

    # Webhooks: tell an external system that something happened here.
    webhook_parser = subparsers.add_parser("webhook", help="Manage webhooks")
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_command", help="Webhook commands")

    wh_list = webhook_subparsers.add_parser("list", help="List webhooks")
    wh_list.add_argument("--global", action="store_true", dest="use_global", help="Server-wide hooks")

    wh_add = webhook_subparsers.add_parser("add", help="Add a webhook")
    wh_add.add_argument("url", help="Receiver URL (http:// or https://)")
    wh_add.add_argument("--event", action="append", dest="events", help="Subscribe to one event (repeatable); default all")
    wh_add.add_argument("--secret", help="Shared secret used to sign deliveries")
    wh_add.add_argument("--global", action="store_true", dest="use_global", help="Server-wide hook")

    wh_remove = webhook_subparsers.add_parser("remove", help="Remove a webhook")
    wh_remove.add_argument("webhook_id", help="Webhook id from 'fox webhook list'")

    wh_ping = webhook_subparsers.add_parser("ping", help="Send a test delivery and report the result")
    wh_ping.add_argument("webhook_id", help="Webhook id")

    wh_deliveries = webhook_subparsers.add_parser("deliveries", help="Show recent delivery attempts")
    wh_deliveries.add_argument("webhook_id", help="Webhook id")
    wh_deliveries.add_argument("--limit", type=int, default=15, help="How many to show")

    # Status checks: what a CI job reports back.
    status_parser = subparsers.add_parser("status-check", help="Report or view commit status checks")
    status_subparsers = status_parser.add_subparsers(dest="status_command", help="Status check commands")

    sc_report = status_subparsers.add_parser("report", help="Report a check result against a commit")
    sc_report.add_argument("context", help="Check name, e.g. ci/unit-tests")
    sc_report.add_argument("state", choices=["pending", "success", "failure", "error"], help="Result")
    sc_report.add_argument("--description", help="Short summary, e.g. '42 passed'")
    sc_report.add_argument("--url", dest="target_url", help="Link to the build log")
    sc_report.add_argument("--commit", help="Commit id (defaults to HEAD)")

    sc_show = status_subparsers.add_parser("show", help="Show checks on a commit")
    sc_show.add_argument("--commit", help="Commit id (defaults to HEAD)")
    sc_show.add_argument("--history", action="store_true", help="Show every report, not just current state")

    # Server-side push rules (pre-receive policy).
    hook_parser = subparsers.add_parser("hook", help="Manage server-side push rules")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", help="Hook commands")

    hk_list = hook_subparsers.add_parser("list", help="List push rules")
    hk_list.add_argument("--global", action="store_true", dest="use_global", help="Server-wide rules")

    hk_add = hook_subparsers.add_parser("add", help="Add a push rule")
    hk_add.add_argument("name", help="A name you will recognise, e.g. 'no-secrets'")
    hk_add.add_argument("--global", action="store_true", dest="use_global", help="Server-wide rule")
    hk_add.add_argument("--min-message-length", type=int, help="Reject shorter commit messages")
    hk_add.add_argument("--message-pattern", help="Regex the commit message must match")
    hk_add.add_argument("--forbid-path", action="append", dest="forbidden_paths",
                        help="Glob that may never be pushed (repeatable)")
    hk_add.add_argument("--protect-path", action="append", dest="protected_paths",
                        help="Glob only listed users may change (repeatable)")
    hk_add.add_argument("--allow-user", action="append", dest="protected_allowed_users",
                        help="User allowed to change protected paths (repeatable)")
    hk_add.add_argument("--max-file-bytes", type=int, help="Per-file ceiling for this repository")
    hk_add.add_argument("--forbid-content", action="append", dest="forbidden_content",
                        help="Regex refused inside text files (repeatable)")
    hk_add.add_argument("--external-url", help="Policy service that answers allow/deny")

    hk_remove = hook_subparsers.add_parser("remove", help="Remove a push rule")
    hk_remove.add_argument("hook_id", help="Rule id from 'fox hook list'")

    hk_enable = hook_subparsers.add_parser("enable", help="Enforce a push rule")
    hk_enable.add_argument("hook_id", help="Rule id")

    hk_disable = hook_subparsers.add_parser("disable", help="Stop enforcing a push rule")
    hk_disable.add_argument("hook_id", help="Rule id")

    hk_test = hook_subparsers.add_parser("test", help="Dry-run a rule against a hypothetical commit")
    hk_test.add_argument("hook_id", help="Rule id")
    hk_test.add_argument("--message", default="", help="Commit message to try")
    hk_test.add_argument("--path", action="append", dest="paths", help="File path to try (repeatable)")

    # SSH key authentication.
    ssh_parser = subparsers.add_parser("ssh", help="SSH key authentication")
    ssh_subparsers = ssh_parser.add_subparsers(dest="ssh_command", help="SSH commands")

    ssh_add = ssh_subparsers.add_parser("add", help="Register a public key on your account")
    ssh_add.add_argument("title", help="A name for this key, e.g. 'work-laptop'")
    ssh_add.add_argument("--key", dest="key_path", help="Path to the .pub file (default: ~/.ssh/id_ed25519.pub)")

    ssh_subparsers.add_parser("list", help="List your registered keys")

    ssh_remove = ssh_subparsers.add_parser("remove", help="Remove a registered key")
    ssh_remove.add_argument("key_id", help="Key id from 'fox ssh list'")

    ssh_login_parser = ssh_subparsers.add_parser("login", help="Sign in with a private key instead of a password")
    ssh_login_parser.add_argument("--username", help="Account to sign in as")
    ssh_login_parser.add_argument("--key", dest="key_path", help="Private key to sign with")

    # Search across repositories, commit messages and file contents.
    search_parser = subparsers.add_parser("search", help="Search repositories, commits and code")
    search_subparsers = search_parser.add_subparsers(dest="search_command", help="Search commands")

    se_repos = search_subparsers.add_parser("repos", help="Find repositories")
    se_repos.add_argument("query", nargs="?", help="Match name, description or owner")
    se_repos.add_argument("--owner", help="Only repositories owned by this user")
    se_repos.add_argument("--limit", type=int, default=50)

    se_commits = search_subparsers.add_parser("commits", help="Find commits by message")
    se_commits.add_argument("query", nargs="?", help="Words from the commit message")
    se_commits.add_argument("--author", help="Only commits by this user")
    se_commits.add_argument("--all", action="store_true", dest="all_repos",
                            help="Search every repository, not just this one")
    se_commits.add_argument("--limit", type=int, default=50)

    se_code = search_subparsers.add_parser("code", help="Find a string in tracked files")
    se_code.add_argument("query", help="The string to look for")
    se_code.add_argument("--repo", help="Repository id (defaults to the linked one)")
    se_code.add_argument("--branch", help="Branch to search (defaults to the head)")
    se_code.add_argument("--path", help="Only files whose path contains this")
    se_code.add_argument("--regex", action="store_true", help="Treat the query as a regex")
    se_code.add_argument("--case-sensitive", action="store_true", dest="case_sensitive")
    se_code.add_argument("--limit", type=int, default=200)

    # Administrative operations reachable over the API.
    admin_parser = subparsers.add_parser("admin", help="Administrative operations")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command", help="Admin commands")

    admin_subparsers.add_parser("lockouts", help="Show locked accounts and failed sign-ins")

    ad_unlock = admin_subparsers.add_parser("unlock", help="Clear an account lockout")
    ad_unlock.add_argument("username", help="Account to unlock")

    admin_subparsers.add_parser("schema", help="Check the live schema against the models")
    admin_subparsers.add_parser("backups", help="List backups recorded on the server")
    link_parser = subparsers.add_parser("link", help="Link local repo to an existing remote (after admin approval)")
    
    args = parser.parse_args()
    
    # Handle global version flag
    if args.version:
        print_version()
        return
    
    # Handle global update flag
    if args.update:
        fox = FoxClient()
        if args.update == "username":
            new_username = input("Enter new username: ").strip()
            if new_username:
                fox.set_global_username(new_username)
            else:
                print("❌ Username cannot be empty")
        return
    
    if not args.command:
        print_extended_help()
        return
    
    # `init` must not discover an enclosing repository: creating a repo inside
    # another one is a deliberate act, and climbing out of the directory the
    # user chose would silently re-initialise the parent instead.
    fox = FoxClient(discover=(args.command != "init"))

    # Commands now run from the repository root, so remap the paths the user
    # typed from wherever they actually were.
    if fox.path_prefix:
        if getattr(args, "files", None):
            args.files = [fox.resolve_user_path(f) for f in args.files]
        for attr in ("path", "paths"):
            value = getattr(args, attr, None)
            if isinstance(value, str):
                setattr(args, attr, fox.resolve_user_path(value))
            elif isinstance(value, list):
                setattr(args, attr, [fox.resolve_user_path(v) for v in value])

    # Handle special commands
    if args.command == "help":
        print_extended_help()
        return
    
    # Override server URL if provided
    if hasattr(args, 'server') and args.server:
        fox.server_url = args.server
    
    # Execute commands
    if args.command == "init":
        # Use environment variable for default username if available
        username = args.username
        if not username and os.getenv('FOXNEST_USER'):
            username = os.getenv('FOXNEST_USER')
        
        server_url = getattr(args, 'server', None)
        if server_url:
            fox.server_url = server_url
            
        fox.init(username, args.repo_name)
        
    elif args.command == "add":
        fox.add(args.files, add_all=args.all)
        
    elif args.command == "commit":
        author = getattr(args, 'author', None)
        if author:
            # Temporarily override author
            config = fox.load_config()
            if config:
                original_username = config.get('username')
                config['username'] = author
                fox.save_config(config)
                fox.commit(args.message)
                config['username'] = original_username
                fox.save_config(config)
            else:
                fox.commit(args.message)
        else:
            fox.commit(args.message)
            
    elif args.command == "push":
        fox.push(
            archive=getattr(args, "archive", False),
            refspec=getattr(args, "refspec", None),
            push_from_branch=getattr(args, "push_from_branch", None),
            push_to_branch=getattr(args, "push_to_branch", None),
        )
        
    elif args.command == "pull":
        fox.pull()
        
    elif args.command == "status":
        if getattr(args, 'short', False):
            fox.status_short()
        else:
            fox.status()
            
    elif args.command == "set":
        if getattr(args, 'set_command', None) == "origin":
            # Check if --global flag is set
            is_global = getattr(args, 'global_origin', False)
            fox.set_origin(args.url, is_global=is_global)
        elif getattr(args, 'set_command', None) == "username":
            fox.set_global_username(args.username)
        elif getattr(args, 'set_command', None) == "repo-id":
            fox.set_repo_id(args.repo_id)
        else:
            print("Usage:")
            print("  fox set origin <url> [--global]  - Set remote origin URL")
            print("  fox set username <name>          - Set global username")
            print("  fox set repo-id <id>             - Link local repo to remote repo_id")
            print("\nExamples:")
            print("  fox set origin 192.168.15.207:502")
            print("  fox set origin 192.168.15.207:502 --global  # Set for all repos")
            print("  fox set username john_doe")
            print("  fox set repo-id bb3b2817c768fe6c")
            
    elif args.command == "config":
        # Show current config
        global_config = fox.load_global_config()
        print("\nGlobal Configuration:")
        print(f"  Username: {global_config.get('username', 'Not set')}")
        print(f"  Origin URL: {global_config.get('origin_url', 'Not set')}")
        print(f"  Config file: {fox.global_config_file}")
        
        if fox.is_initialized():
            local_config = fox.load_config()
            print("\nLocal Repository Configuration:")
            print(f"  Username: {local_config.get('username')}")
            print(f"  Repository: {local_config.get('repo_name')}")
            print(f"  Server URL: {local_config.get('server_url')}")
            print(f"  Origin URL: {local_config.get('origin_url', 'Not set (using global)')}")
            
    elif args.command == "log":
        if getattr(args, 'oneline', False):
            fox.log_oneline(getattr(args, 'max_count', None))
        else:
            fox.log(getattr(args, 'max_count', None))

    elif args.command == "branch":
        if getattr(args, "branch_command", None) == "list" or args.branch_command is None:
            fox.list_branches()
        elif args.branch_command == "create":
            fox.create_branch(
                args.name,
                from_commit=args.from_commit,
                from_branch=getattr(args, "from_branch", None),
                checkout=args.checkout,
            )
        elif args.branch_command == "delete":
            fox.delete_branch(args.name)
        else:
            print("Usage: fox branch [list|create|delete]")

    elif args.command == "checkout":
        if getattr(args, "create", False):
            fox.create_branch(args.branch, checkout=True)
        else:
            fox.checkout_branch(args.branch)

    elif args.command == "checkout-files":
        if getattr(args, "checkout_files_server_commit", False):
            fox.checkout_files_server_commit(
                args.checkout_files_from,
                list(args.paths),
            )
        else:
            fox.checkout_files(
                args.checkout_files_from,
                list(args.paths),
                prefer_server=getattr(args, "checkout_files_server", False),
            )

    elif args.command == "merge":
        fox.merge_branch(args.branch)

    elif args.command == "compare":
        fox.compare(
            args.from_ref,
            args.to_ref,
            path=getattr(args, "compare_path", None),
            as_json=getattr(args, "compare_json", False),
        )

    elif args.command == "file-history":
        fox.file_history(
            args.path,
            branch=getattr(args, "branch", None),
            lines_range=getattr(args, "file_history_lines", None),
            as_json=getattr(args, "file_history_json", False),
        )

    elif args.command == "delete-commit":
        fox.delete_commit(
            args.commit,
            branch=getattr(args, "branch", None),
            local_only=getattr(args, "delete_commit_local_only", False),
            expected_head=getattr(args, "delete_commit_expected_head", None),
        )

    elif args.command == "rollback":
        fox.rollback(args.commit_id)

    elif args.command == "tag":
        if getattr(args, "tag_command", None) == "list" or args.tag_command is None:
            fox.list_tags()
        elif args.tag_command == "create":
            fox.create_tag(args.name, commit_id=args.commit_id, message=args.message)
        elif args.tag_command == "delete":
            fox.delete_tag(args.name)

    elif args.command == "release":
        if getattr(args, "release_command", None) == "list" or args.release_command is None:
            fox.list_releases()
        elif args.release_command == "create":
            fox.create_release(args.version, tag=args.tag, title=args.title, notes=args.notes)

    elif args.command == "pr":
        if getattr(args, "pr_command", None) == "list" or args.pr_command is None:
            fox.list_pull_requests()
        elif args.pr_command == "create":
            fox.create_pull_request(args.title, args.source, args.target, description=args.description)
        elif args.pr_command == "close":
            fox.close_pull_request(args.id)
        elif args.pr_command == "merge":
            fox.merge_pull_request(args.id)
        elif args.pr_command == "resolve":
            fox.pr_resolve(args.id, session_id=getattr(args, "session_id", None), submit=getattr(args, "submit", False), directory=getattr(args, "directory", None))
    
    elif args.command == "gc":
        fox.gc()

    elif args.command == "login":
        # Attempt login; prefer CLI args but fall back to interactive prompt
        uname = getattr(args, 'username', None)
        pwd = getattr(args, 'password', None)
        fox.login(username=uname, password=pwd)
    elif args.command == "logout":
        fox.logout()

    elif args.command == "token":
        sub = getattr(args, "token_command", None)
        if sub == "create":
            fox.token_create(
                args.name,
                scopes=getattr(args, "scopes", None),
                expires_in_days=getattr(args, "expires_in_days", None),
                use=getattr(args, "use", False),
            )
        elif sub == "list":
            fox.token_list(include_revoked=getattr(args, "include_revoked", False))
        elif sub == "revoke":
            fox.token_revoke(args.token_id)
        elif sub == "use":
            fox.token_use(args.token)
        else:
            print("Usage: fox token <create|list|revoke|use> ...")

    elif args.command == "webhook":
        sub = getattr(args, "webhook_command", None)
        if sub == "list":
            fox.webhook_list(use_global=getattr(args, "use_global", False))
        elif sub == "add":
            fox.webhook_add(
                args.url,
                events=getattr(args, "events", None),
                secret=getattr(args, "secret", None),
                use_global=getattr(args, "use_global", False),
            )
        elif sub == "remove":
            fox.webhook_remove(args.webhook_id)
        elif sub == "ping":
            fox.webhook_ping(args.webhook_id)
        elif sub == "deliveries":
            fox.webhook_deliveries(args.webhook_id, limit=getattr(args, "limit", 15))
        else:
            print("Usage: fox webhook <list|add|remove|ping|deliveries> ...")

    elif args.command == "status-check":
        sub = getattr(args, "status_command", None)
        if sub == "report":
            fox.status_report(
                args.context, args.state,
                description=getattr(args, "description", None),
                target_url=getattr(args, "target_url", None),
                commit=getattr(args, "commit", None),
            )
        elif sub == "show":
            fox.status_show(
                commit=getattr(args, "commit", None),
                history=getattr(args, "history", False),
            )
        else:
            print("Usage: fox status-check <report|show> ...")

    elif args.command == "hook":
        sub = getattr(args, "hook_command", None)
        if sub == "list":
            fox.hook_list(use_global=getattr(args, "use_global", False))
        elif sub == "add":
            fox.hook_add(
                args.name,
                use_global=getattr(args, "use_global", False),
                min_message_length=getattr(args, "min_message_length", None),
                message_pattern=getattr(args, "message_pattern", None),
                forbidden_paths=getattr(args, "forbidden_paths", None),
                protected_paths=getattr(args, "protected_paths", None),
                protected_allowed_users=getattr(args, "protected_allowed_users", None),
                max_file_bytes=getattr(args, "max_file_bytes", None),
                forbidden_content=getattr(args, "forbidden_content", None),
                external_url=getattr(args, "external_url", None),
            )
        elif sub == "remove":
            fox.hook_remove(args.hook_id)
        elif sub == "enable":
            fox.hook_enable(args.hook_id, True)
        elif sub == "disable":
            fox.hook_enable(args.hook_id, False)
        elif sub == "test":
            fox.hook_test(args.hook_id, message=getattr(args, "message", ""),
                          paths=getattr(args, "paths", None))
        else:
            print("Usage: fox hook <list|add|remove|enable|disable|test> ...")

    elif args.command == "ssh":
        sub = getattr(args, "ssh_command", None)
        if sub == "add":
            fox.ssh_key_add(args.title, key_path=getattr(args, "key_path", None))
        elif sub == "list":
            fox.ssh_key_list()
        elif sub == "remove":
            fox.ssh_key_remove(args.key_id)
        elif sub == "login":
            fox.ssh_login(
                username=getattr(args, "username", None),
                key_path=getattr(args, "key_path", None),
            )
        else:
            print("Usage: fox ssh <add|list|remove|login> ...")

    elif args.command == "search":
        sub = getattr(args, "search_command", None)
        if sub == "repos":
            fox.search_repos(query=getattr(args, "query", None),
                             owner=getattr(args, "owner", None),
                             limit=getattr(args, "limit", 50))
        elif sub == "commits":
            fox.search_commits_cmd(query=getattr(args, "query", None),
                                   author=getattr(args, "author", None),
                                   all_repos=getattr(args, "all_repos", False),
                                   limit=getattr(args, "limit", 50))
        elif sub == "code":
            fox.search_code_cmd(args.query,
                                branch=getattr(args, "branch", None),
                                path=getattr(args, "path", None),
                                regex=getattr(args, "regex", False),
                                case_sensitive=getattr(args, "case_sensitive", False),
                                repo=getattr(args, "repo", None),
                                limit=getattr(args, "limit", 200))
        else:
            print("Usage: fox search <repos|commits|code> ...")

    elif args.command == "admin":
        sub = getattr(args, "admin_command", None)
        if sub == "lockouts":
            fox.admin_lockouts()
        elif sub == "unlock":
            fox.admin_unlock(args.username)
        elif sub == "schema":
            fox.admin_schema()
        elif sub == "backups":
            fox.admin_backups()
        else:
            print("Usage: fox admin <lockouts|unlock|schema|backups>")

    elif args.command == "link":
        if not fox.check_repository("link"):
            return
        if not fox.get_auth_headers():
            print("Run: fox login")
            if not fox.login():
                return
        config = fox.load_config()
        origin = fox.get_origin_url()
        if origin:
            config["server_url"] = origin
            fox.server_url = origin
        rid = fox.link_remote_repository(config)
        if rid:
            print("✅ Ready to push. Run: fox push")
        else:
            print("❌ Could not find remote repository on server.")
            print("   Ensure admin approved repo creation, then retry.")
            print("   Or set manually: fox set repo-id <id-from-web-ui>")
    
    elif args.command == "docs":
        # Generate docs locally
        try:
            from docs_generator import generate_docs, get_available_doc_targets
            project_path = Path(args.path).resolve()
            output_dir = args.output if args.output else project_path / "docs"
            available_docs = get_available_doc_targets(str(project_path))

            if getattr(args, "list_docs", False):
                print("\n📚 Available local document names:")
                for name in available_docs:
                    print(f"  - {name}")
                print("\nUse: fox docs --docs <name1,name2,...>")
                print("Or:  fox docs --all-docs")
                return

            requested_docs = _parse_docs_args(getattr(args, "docs", None))
            selected_docs = None
            if not getattr(args, "all_docs", False) and requested_docs:
                invalid = [doc for doc in requested_docs if doc != "all" and doc not in available_docs]
                if invalid:
                    print(f"❌ Invalid local doc names: {', '.join(invalid)}")
                    print(f"   Available: {', '.join(available_docs)}")
                    return
                selected_docs = requested_docs
            
            print(f"\n📚 Generating documentation for: {project_path}")
            print(f"📁 Output directory: {output_dir}")
            if getattr(args, "model", None):
                os.environ["FOXNEST_DOCS_MODEL"] = args.model
                try:
                    import llm_helper as _lh
                    _lh._llm_helper = None
                except Exception:
                    pass
                print(f"🤖 Model: {args.model}")
            if selected_docs:
                print(f"🧩 Selected docs: {', '.join(selected_docs)}")
            else:
                print("🧩 Selected docs: all")
            
            result = generate_docs(str(project_path), str(output_dir), selected_docs=selected_docs)
            
            if not result.get("error"):
                generated_languages = [lang for lang, stats in result.items() if isinstance(stats, dict)]
                analyzed_files = sum(stats.get("files", 0) for stats in result.values() if isinstance(stats, dict))
                print(f"\n✅ Documentation generated successfully!")
                print(f"📄 Files analyzed: {analyzed_files}")
                if generated_languages:
                    print(f"💻 Language docs generated: {', '.join(generated_languages)}")
                print(f"\n📖 Documentation saved to: {output_dir}")
            else:
                print(f"\n❌ Error: {result.get('error', 'Unknown error')}")
                if result.get("available_docs"):
                    print(f"   Available docs: {', '.join(result.get('available_docs', []))}")
        except ImportError:
            print("❌ Error: docs_generator module not found. Please ensure docs_generator.py is in the same directory.")
        except Exception as e:
            print(f"❌ Error generating documentation: {str(e)}")
    
    elif args.command == "generate-docs":
        # Generate docs on server
        try:
            if getattr(args, "list_docs", False):
                _print_project_doc_options()
                print("\nUse: fox generate-docs --docs readme,api,architecture")
                print("Or:  fox generate-docs --all-docs")
                return

            raw_selected_docs = _parse_docs_args(getattr(args, "docs", None))
            selected_doc_keys = None
            if not getattr(args, "all_docs", False) and raw_selected_docs:
                selected_doc_keys, invalid = _normalize_project_doc_keys(raw_selected_docs)
                if invalid:
                    print(f"❌ Invalid project doc names: {', '.join(invalid)}")
                    _print_project_doc_options()
                    return

            # Get repo_id from args or local config
            repo_id = args.repo_id
            if not repo_id:
                if not fox.is_initialized():
                    print("❌ Not a FoxNest repository. Run 'fox init' first.")
                    return
                config = fox.load_config()
                repo_id = config.get('repo_id')
                if not repo_id:
                    print("❌ Repository ID not found. Please specify with --repo-id or push your repository first.")
                    return
            
            server_url = fox._resolve_server_url()
            print(f"\n📚 Requesting documentation generation on server...")
            print(f"🌐 Server: {server_url}")
            print(f"🔗 Repository ID: {repo_id}")
            selected_model = getattr(args, 'model', None) or os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")
            print(f"🤖 Model: {selected_model}")
            if selected_doc_keys:
                selected_doc_names = [_PROJECT_DOC_KEY_TO_NAME[key] for key in selected_doc_keys]
                print(f"🧩 Selected docs: {', '.join(selected_doc_names)}")
            else:
                print("🧩 Selected docs: all")

            if not fox.ensure_authenticated():
                print("❌ Login required. Run: fox login")
                return

            print("\n📤 Pushing local commits to server (docs use committed server code)...")
            fox.push()

            # Call server API (authenticated)
            params = {"llm_model": selected_model}
            if selected_doc_keys:
                params["selected_docs"] = ",".join(selected_doc_keys)

            try:
                response = fox._authed_post(
                    f"{fox._resolve_server_url()}/api/repository/{repo_id}/generate-project-docs",
                    params=params,
                    timeout=300,
                )
            except requests.exceptions.RequestException as e:
                print(f"❌ Request failed: {e}")
                return
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success") is False:
                    print(f"❌ Server error: {result.get('error', 'Unknown error')}")
                    return

                status = str(result.get("status", "")).lower()
                if status in {"processing", "queued", "running"}:
                    status_url = result.get("status_url") or f"/api/repository/{repo_id}/docs-status"
                    if status_url.startswith("/"):
                        status_url = f"{fox._resolve_server_url()}{status_url}"

                    print("\n⏳ Documentation generation started on server.")
                    print("   Qwen is reading and understanding all your code files.")
                    print("   This can take 10–30 minutes depending on project size.")
                    print("   Waiting for completion...\n")
                    completed = False
                    status_data = {}
                    # Poll every 15 seconds for up to 90 minutes
                    for i in range(360):
                        try:
                            status_resp = fox._authed_get(status_url, timeout=15)
                            if status_resp.status_code == 200:
                                status_data = status_resp.json()
                                job_status = str(status_data.get("status", "")).lower()
                                if job_status == "completed":
                                    completed = True
                                    break
                                if job_status == "failed":
                                    print(f"\n❌ Docs generation failed: {status_data.get('error', 'Unknown error')}")
                                    return
                                if job_status == "not_started" and i > 4 and not status_data.get("docs_available"):
                                    print("\n⚠️ Server lost job state (restart?). Check docs-status later or retry.")
                                    return
                                progress = status_data.get("docs_progress") or []
                                active = next((p for p in progress if p.get("status") == "generating"), None)
                                if active and i % 4 == 0:
                                    print(f"   📝 Working on: {active.get('name')}")
                        except requests.exceptions.RequestException as e:
                            if i % 8 == 0:
                                print(f"   ⚠ Poll error: {e}")
                        # Print progress every 4 polls (~1 minute)
                        if i % 4 == 0:
                            elapsed_min = (i * 15) // 60
                            print(f"   ⏱  {elapsed_min} min elapsed — still generating docs...")
                        time.sleep(15)

                    if not completed:
                        print("\n⚠️ Docs generation is taking very long (>90 min).")
                        print(f"   You can check status later: {status_url}")
                        print(f"   When done, run: fox pull   to sync them locally.")
                        return

                    # Use data from the status response (do NOT re-POST)
                    job_result = status_data.get("result") or {}
                    result = {
                        "llm_model": job_result.get("llm_model", selected_model),
                        "project_info": job_result.get("project_info", {}),
                        "documents": job_result.get("documents", []),
                        "skipped_docs": job_result.get("skipped_docs", []),
                        "docs_url": job_result.get("docs_url", f"/api/repository/{repo_id}/docs/project"),
                    }

                print(f"\n✅ Documentation generated on server!")
                print(f"🤖 Model used: {result.get('llm_model', selected_model)}")

                project_info = result.get("project_info", {})
                if project_info:
                    print(f"\n📊 Project Analysis:")
                    if project_info.get("files") is not None:
                        print(f"📄 Files scanned: {project_info['files']}")
                    if project_info.get("lines") is not None:
                        print(f"🧾 Lines scanned: {project_info['lines']}")
                    if project_info.get("languages"):
                        print(f"💻 Languages detected: {', '.join(project_info['languages'])}")

                print(f"\n📖 You can view the docs on the server or download them")
                
                # Optionally list available docs
                print("\n📁 Generated documentation files:")
                docs = result.get('documents', [])
                for doc in docs[:10]:
                    print(f"  • {doc}")
                if len(docs) > 10:
                    print(f"  ... and {len(docs) - 10} more files")
                print(f"\n🔗 Index: {fox._resolve_server_url()}/api/repository/{repo_id}/docs/project/index.md")

                print("\n🔄 Syncing generated docs to local repository...")
                if fox.pull():
                    print("✅ Local repository updated with generated docs")
                else:
                    print("⚠️ Docs were generated on server, but local sync failed. Run: fox pull")
            else:
                error_msg = fox._response_error_message(response)
                print(f"❌ Server error: {error_msg}")
                if response.status_code == 401:
                    print(f"\n💡 Credentials file: {fox.global_config_file}")
                    print(f"   Server used: {server_url}")
                    if fox.has_auth_token():
                        print("   A token is saved but was rejected (wrong server, expired, or outdated fox.exe).")
                        print("   Run: fox login")
                    else:
                        print("   No access_token found. Run: fox login")
                    print("   If you use fox.exe from an installer, rebuild/reinstall from the latest client/client1/fox.py")
                if response.status_code == 500 and "Repository not found" in str(error_msg):
                    print("\n🔧 Recovery steps:")
                    print("   1) Verify you are in the correct fox repo folder")
                    print("   2) Run: fox push")
                    print("      (this recreates/links the remote repository and updates repo_id)")
                    print("   3) Run: fox generate-docs")
        except requests.exceptions.Timeout:
            print("⏰ Request timed out after 5 minutes.")
            print("   Project-docs generation can take longer for large repositories.")
            print(f"   Check docs at: {fox._resolve_server_url()}/api/repository/{repo_id}/docs/project/index.md")
        except requests.exceptions.RequestException as e:
            print(f"❌ Connection error: {str(e)}")
        except Exception as e:
            print(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    main()
