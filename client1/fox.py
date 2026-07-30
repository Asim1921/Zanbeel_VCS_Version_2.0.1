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
from datetime import datetime
from pathlib import Path
import requests

class FoxClient:
    def __init__(self):
        self.fox_dir = Path(".fox")
        self.config_file = self.fox_dir / "config.json"
        self.staging_dir = self.fox_dir / "staging"
        self.objects_dir = self.fox_dir / "objects"
        self.packs_dir = self.fox_dir / "packs"  # Git-like pack files
        self.commits_file = self.fox_dir / "commits.json"
        self.head_file = self.fox_dir / "HEAD"
        self.index_file = self.fox_dir / "index.json"  # Git-like index for fast tracking
        self.delta_cache_file = self.fox_dir / "delta_cache.json"  # Cache for delta relationships
        
        # Global credentials file (in user's home directory)
        self.global_config_dir = Path.home() / ".foxnest"
        self.global_config_file = self.global_config_dir / "credentials.json"
        
        # Compression settings
        self.compression_level = 6  # zlib compression level (1-9)
        self.pack_threshold = 20  # Number of objects before creating pack
        
        # Default server configuration
        self.server_url = "http://192.168.0.11:5000"
        
        # Load server URL from config if available
        if self.is_initialized():
            config = self.load_config()
            if config and 'server_url' in config:
                self.server_url = config['server_url']
    
    def load_global_config(self):
        """Load global FoxNest credentials"""
        if not self.global_config_file.exists():
            return {}
        
        try:
            with open(self.global_config_file, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load global config: {e}")
            return {}
    
    def save_global_config(self, config):
        """Save global FoxNest credentials"""
        self.global_config_dir.mkdir(exist_ok=True)
        
        try:
            with open(self.global_config_file, "w") as f:
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
    
    def check_repository(self, command_name="command"):
        """Check if current directory is a Fox repository"""
        if not self.is_initialized():
            current_dir = Path.cwd().name
            print(f"Fatal: {current_dir} not a repository, run 'fox init' to initialize a new repository")
            return False
        return True
    
    def get_origin_url(self):
        """Get the origin URL from config (local or global)"""
        # First try local config
        if self.is_initialized():
            config = self.load_config()
            if config and config.get("origin_url"):
                return config["origin_url"]
        
        # Fall back to global config
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
                origin_url = f"http://{origin_url}:5000"
        
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
            self.save_config(config)
            
            print(f"Origin set to: {origin_url}")
            return True
    
    def set_global_origin(self, origin_url):
        """Set global origin URL for all Fox repositories"""
        return self.set_origin(origin_url, is_global=True)
    
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
        
        print(f"Initialized Fox repository for {username}/{repo_name}")
        print("Use 'fox add <files>' to add files and 'fox commit' to commit changes")
        return True
    
    def is_initialized(self):
        """Check if repository is initialized"""
        return self.fox_dir.exists() and self.config_file.exists()
    
    def load_config(self):
        """Load repository configuration"""
        if not self.is_initialized():
            print("Not a Fox repository! Run 'fox init' first.")
            return None
        
        with open(self.config_file, "r") as f:
            return json.load(f)
    
    def save_config(self, config):
        """Save repository configuration"""
        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)
    
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
            return  # Not enough objects to pack
        
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
            with open(self.commits_file, "r") as f:
                commits = json.load(f)
            
            if not commits:
                return
            
            # Get the latest commit and update index
            latest_commit = commits[-1]
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
        """Get all files in the working directory (excluding .fox directory and common ignore patterns)"""
        all_files = []
        current_dir = Path(".")
        
        # Common directories to ignore
        ignore_patterns = {
            ".fox", ".git", ".svn", ".hg",
            "venv", "env", ".venv", ".env",
            "node_modules", "__pycache__", ".pytest_cache",
            ".tox", ".coverage", "dist", "build",
            ".DS_Store", "Thumbs.db"
        }
        
        for file_path in current_dir.rglob("*"):
            if file_path.is_file():
                # Check if any part of the path contains ignored patterns
                path_parts = file_path.parts
                should_ignore = False
                
                for part in path_parts:
                    if part in ignore_patterns or part.startswith('.'):
                        should_ignore = True
                        break
                
                if not should_ignore:
                    all_files.append(file_path)
        
        return all_files
    
    def get_last_commit_files(self):
        """Get file states from the last commit"""
        try:
            if not self.commits_file.exists():
                return {}
            
            with open(self.commits_file, "r") as f:
                commits = json.load(f)
            
            if not commits:
                return {}
            
            # Get the latest commit and build path-to-hash mapping
            latest_commit = commits[-1]
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
    
    def add(self, files, add_all=False):
        """Add files to staging area"""
        if not self.check_repository("add"):
            return False

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
            file_paths = list(Path(".").glob(file_pattern))
            if not file_paths:
                file_paths = [Path(file_pattern)]
            
            for filepath in file_paths:
                if filepath.exists() and filepath.is_file():
                    # Skip .fox directory files
                    if ".fox" in str(filepath):
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
                    
                    # Add to staging
                    staging_file = self.staging_dir / filepath.name
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
    
    def commit(self, message):
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
        
        # Get parent commit
        parent = None
        if self.head_file.exists():
            with open(self.head_file, "r") as f:
                parent = f.read().strip()
        
        # Collect staged files (path -> {hash, content})
        staged_files_data = {}
        for staging_file in staged_files:
            with open(staging_file, "r") as f:
                file_info = json.load(f)
            
            file_hash = file_info["hash"]
            file_path = file_info["path"]
            
            # Try to load compressed file content
            content = self.load_object_compressed(file_hash)
            
            if content is not None:
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
            "files": files
        }
        
        # Save commit locally
        commits.append(commit)
        with open(self.commits_file, "w") as f:
            json.dump(commits, f, indent=2)
        
        # Update HEAD
        with open(self.head_file, "w") as f:
            f.write(commit_id)
        
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
        print(f"Files: {len(files)}")
        
        return True
    
    def create_remote_repository(self, config):
        """Create repository on remote server"""
        try:
            response = requests.post(
                f"{config['server_url']}/api/repository/create",
                json={
                    "username": config["username"],
                    "repo_name": config["repo_name"]
                },
                timeout=10
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
            else:
                print(f"HTTP error: {response.status_code}")
        
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
        
        return None
    
    def get_existing_repository_id(self, config):
        """Get the ID of an existing repository on the server"""
        try:
            response = requests.get(
                f"{config['server_url']}/api/repository/list",
                params={
                    "username": config["username"],
                    "repo_name": config["repo_name"]
                },
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data["success"] and data.get("repositories"):
                    # Find the repository with matching name
                    for repo in data["repositories"]:
                        if repo["name"] == config["repo_name"]:
                            return repo["id"]
            
        except requests.exceptions.RequestException as e:
            print(f"Network error while getting repository ID: {e}")
        
        return None
    
    def pull_existing_repository(self, config):
        """Pull all commits from an existing repository"""
        try:
            response = requests.get(
                f"{config['server_url']}/api/repository/{config['repo_id']}/commits",
                params={"full": "true"},
                timeout=30
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

    def push(self, archive=False):
        """Push commits to remote repository"""
        if not self.check_repository("push"):
            return False
        
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
        
        # Create remote repository if needed
        if not config.get("repo_id"):
            print("Creating remote repository...")
            result = self.create_remote_repository(config)
            
            # Handle different return types
            if isinstance(result, dict) and result.get("status") == "pending":
                # Repository creation is pending approval
                team_lead = result.get("team_lead", "team lead")
                
                # Try to check if it was already approved by checking with team lead's username
                import hashlib
                expected_repo_id = hashlib.md5(f"{team_lead}_{config['repo_name']}".encode()).hexdigest()[:16]
                
                try:
                    verify_response = requests.get(
                        f"{config['server_url']}/api/repository/{expected_repo_id}",
                        timeout=10
                    )
                    if verify_response.status_code == 200:
                        data = verify_response.json()
                        if data.get("success"):
                            # Repository exists! It was approved
                            print("\n✓ Repository has been approved!")
                            config["repo_id"] = expected_repo_id
                            self.save_config(config)
                            print(f"Configured remote repository: {expected_repo_id}")
                        else:
                            print("\n⏳ Cannot push until repository is approved by team lead.")
                            return False
                    else:
                        print("\n⏳ Cannot push until repository is approved by team lead.")
                        return False
                except Exception as e:
                    print(f"\n⏳ Cannot push until repository is approved by team lead.")
                    return False
            elif not result:
                print("Failed to create remote repository")
                return False
            else:
                # Got repo_id directly
                config["repo_id"] = result
                self.save_config(config)
                print(f"Created remote repository: {result}")
        
        # Load local commits
        if not self.commits_file.exists():
            print("Everything up-to-date")
            return True
        
        with open(self.commits_file, "r") as f:
            commits = json.load(f)
        
        if not commits:
            print("Everything up-to-date")
            return True
        
        # Check if there are actually new commits to push
        # by comparing with remote
        commits_to_push = commits  # Default: push all
        try:
            response = requests.get(
                f"{config['server_url']}/api/repository/{config['repo_id']}/commits",
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    remote_commits = data.get("commits", [])
                    remote_commit_ids = {c["id"] for c in remote_commits}
                    
                    # Filter to only new commits
                    commits_to_push = [c for c in commits if c["id"] not in remote_commit_ids]
                    
                    if not commits_to_push:
                        print("Everything up-to-date")
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
        
        # Push each new commit
        pushed_count = 0
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
                
                commit_data["files"] = server_files
                
                response = requests.post(
                    f"{config['server_url']}/api/repository/{config['repo_id']}/push",
                    json={"commit": commit_data, "archive": archive},
                    timeout=30
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data["success"]:
                        # Check if commit is pending approval
                        if data.get("status") == "pending_approval":
                            team_lead = data.get("team_lead", "team lead")
                            print(f"📝 Commit {commit['id'][:12]} - Pending approval by {team_lead}")
                            if data.get("message"):
                                print(f"   {data['message']}")
                        else:
                            print(f"✓ Pushed commit: {commit['id'][:12]}")
                        pushed_count += 1
                    else:
                        print(f"Failed to push commit {commit['id']}: {data.get('error')}")
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
            print("Everything up-to-date")
        elif archive:
            print("All commits pushed successfully and repository archived!")
        else:
            print("All commits pushed successfully!")
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
        
        if not config.get("repo_id"):
            print("No remote repository configured")
            return False
        
        try:
            # Get current HEAD
            since_commit = None
            if self.head_file.exists():
                with open(self.head_file, "r") as f:
                    since_commit = f.read().strip()
            
            # Pull from server
            params = {"since": since_commit} if since_commit else {}
            response = requests.get(
                f"{config['server_url']}/api/repository/{config['repo_id']}/pull",
                params=params,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                if data["success"]:
                    commits = data["commits"]
                    
                    if not commits:
                        print("Already up to date")
                        return True
                    
                    # Update local repository with pulled commits
                    for commit in commits:
                        # Save files from commit with compression
                        for file_hash, content in commit["files"].items():
                            # Decode base64 content
                            decoded_content = base64.b64decode(content)
                            # Store with compression using new structure
                            self.store_object_compressed(decoded_content, file_hash)
                    
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
                        with open(self.head_file, "w") as f:
                            f.write(data["head"])
                    
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
        if self.head_file.exists():
            with open(self.head_file, "r") as f:
                head = f.read().strip()
            print(f"Current commit: {head}")
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
            print("  (use \"fox checkout -- <file>...\" to discard changes in working directory)")
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
    
    def log(self, max_count=None):
        """Show commit history"""
        if not self.check_repository("log"):
            return False
        
        if not self.commits_file.exists():
            print("No commits yet")
            return True
        
        with open(self.commits_file, "r") as f:
            commits = json.load(f)
        
        if not commits:
            print("No commits yet")
            return True
        
        print("Commit history:")
        commits_to_show = commits
        if max_count:
            commits_to_show = commits[-max_count:]
        
        for commit in reversed(commits_to_show):  # Most recent first
            print(f"\nCommit: {commit['id']}")
            print(f"Author: {commit['author']}")
            print(f"Date: {commit['timestamp']}")
            print(f"Message: {commit['message']}")
            if commit.get('parent'):
                print(f"Parent: {commit['parent']}")
            print(f"Files: {len(commit['files'])}")
        
        return True
    
    def log_oneline(self, max_count=None):
        """Show commit history in one-line format"""
        if not self.check_repository("log"):
            return False
        
        if not self.commits_file.exists():
            print("No commits yet")
            return True
        
        with open(self.commits_file, "r") as f:
            commits = json.load(f)
        
        if not commits:
            print("No commits yet")
            return True
        
        commits_to_show = commits
        if max_count:
            commits_to_show = commits[-max_count:]
        
        for commit in reversed(commits_to_show):  # Most recent first
            date = commit['timestamp'][:10]  # Just the date part
            print(f"{commit['id']} {date} {commit['author']}: {commit['message']}")
        
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
    print("Version: 1.0.0")
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
    print("  push                    Push commits to server")
    print("  push --archive          Push and archive repository")
    print("  pull                    Pull commits from server") 
    print("  status                  Show repository status")
    print("  log                     Show commit history")
    print("  config                  Show configuration")
    print("  gc                      Optimize repository (garbage collection)")
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
    print("  fox --update username    # Change your username")
    print("  fox config               # View current settings")
    print("  fox push --archive")
    print("  fox pull")
    print("  fox status")
    print("  fox log --oneline")
    print("  fox gc                  # Optimize repository storage")
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
    init_parser.add_argument("--server", help="Server URL (default: http://localhost:5000)")
    
    # Add command  
    add_parser = subparsers.add_parser("add", help="Add files to staging area")
    add_parser.add_argument("files", nargs="*", help="Files to add (supports wildcards)")
    add_parser.add_argument("--all", "-A", action="store_true", help="Add all files in working directory")
    
    # Commit command
    commit_parser = subparsers.add_parser("commit", help="Commit staged changes")
    commit_parser.add_argument("-m", "--message", required=True, help="Commit message")
    commit_parser.add_argument("--author", help="Override commit author")
    
    # Push command
    push_parser = subparsers.add_parser("push", help="Push commits to server")
    push_parser.add_argument("--force", action="store_true", help="Force push (use with caution)")
    push_parser.add_argument("--archive", action="store_true", help="Archive repository after push")
    
    # Pull command
    pull_parser = subparsers.add_parser("pull", help="Pull commits from server")
    pull_parser.add_argument("--force", action="store_true", help="Force pull (overwrite local changes)")
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Show repository status")
    status_parser.add_argument("--short", "-s", action="store_true", help="Show short status")
    
    # Log command
    log_parser = subparsers.add_parser("log", help="Show commit history")
    log_parser.add_argument("--oneline", action="store_true", help="Show one line per commit")
    log_parser.add_argument("-n", "--max-count", type=int, help="Limit number of commits shown")
    
    # Set command (for setting origin and username)
    set_parser = subparsers.add_parser("set", help="Set repository configuration")
    set_subparsers = set_parser.add_subparsers(dest="set_command", help="Set commands")
    origin_parser = set_subparsers.add_parser("origin", help="Set remote origin URL")
    origin_parser.add_argument("url", help="Remote origin URL (e.g., 192.168.15.207:502)")
    origin_parser.add_argument("--global", dest="global_origin", action="store_true", help="Set origin globally for all repositories")
    username_parser = set_subparsers.add_parser("username", help="Set global username")
    username_parser.add_argument("username", help="Your username for commits")
    
    # Config command (show configuration)
    config_parser = subparsers.add_parser("config", help="Show FoxNest configuration")
    
    # Garbage collection command
    gc_parser = subparsers.add_parser("gc", help="Optimize repository (garbage collection)")
    
    # Help command
    help_parser = subparsers.add_parser("help", help="Show help information")
    
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
    
    fox = FoxClient()
    
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
        fox.push(archive=getattr(args, 'archive', False))
        
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
        else:
            print("Usage:")
            print("  fox set origin <url> [--global]  - Set remote origin URL")
            print("  fox set username <name>          - Set global username")
            print("\nExamples:")
            print("  fox set origin 192.168.15.207:502")
            print("  fox set origin 192.168.15.207:502 --global  # Set for all repos")
            print("  fox set username john_doe")
            
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
    
    elif args.command == "gc":
        fox.gc()

if __name__ == "__main__":
    main()
