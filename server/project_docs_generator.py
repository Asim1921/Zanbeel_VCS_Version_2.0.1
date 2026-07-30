"""
FoxNest Project Documentation Generator with Qwen LLM
Generates comprehensive project-level documentation:
- README.md
- API Documentation  
- Architecture Document
- Installation Guide
- Tech Stack Document
"""

import os
import re
import json
import hashlib
import time
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
import requests

import docx_utils


EXCLUDED_DIR_NAMES = {
    '.git', 'node_modules', 'venv', '.venv', '__pycache__', 'build', 'dist', 'docs',
    'vendor', 'site-packages', '.mypy_cache', '.pytest_cache', '.tox', '.idea', '.vscode',
    '.next', '.nuxt', '.cache', '.fox', '_archived_unused_20260213_043002'
}


def _has_excluded_part(path_obj: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path_obj.parts)


# ---------------------------------------------------------------------------
# Document Template Support
# ---------------------------------------------------------------------------
# Templates live in  <repo>/docs/templates/  relative to this file's location.
# Each template is a Markdown file that defines the required section structure
# and formatting for the corresponding generated document type.
# The LLM is instructed to follow the template's section headings and layout
# while filling in content derived exclusively from repository evidence.
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "docs" / "templates"
_TEMPLATE_CACHE: Dict[str, str] = {}
_CODE_CONTEXT_CHAR_LIMIT = 200000


_PROJECT_DOC_ALIAS_TO_NAME: Dict[str, str] = {
    "readme": "README.md",
    "readme.md": "README.md",
    "api": "API Documentation",
    "api_documentation": "API Documentation",
    "api documentation": "API Documentation",
    "architecture": "Architecture",
    "installation": "Installation Guide",
    "installation_guide": "Installation Guide",
    "installation guide": "Installation Guide",
    "database": "Database Schema",
    "database_schema": "Database Schema",
    "database schema": "Database Schema",
    "requirements": "Requirements (SRS)",
    "requirements_srs": "Requirements (SRS)",
    "requirements (srs)": "Requirements (SRS)",
    "use_cases": "Use Cases",
    "use cases": "Use Cases",
    "release_notes": "Release Notes",
    "release notes": "Release Notes",
    "user_manual": "User Manual",
    "user manual": "User Manual",
    "api_usage": "API Usage Guide",
    "api usage": "API Usage Guide",
    "api_usage_guide": "API Usage Guide",
    "database_er": "Database ER",
    "database er": "Database ER",
    "index": "index.md",
    "index.md": "index.md",
}


def parse_selected_project_docs(selected_docs: Optional[List[str]]) -> tuple[Optional[set], List[str]]:
    """Parse project-doc selection values into canonical document names.

    Returns (selected_doc_names_or_none, invalid_tokens).
    """
    if not selected_docs:
        return None, []

    tokens: List[str] = []
    for item in selected_docs:
        if item is None:
            continue
        tokens.extend(str(item).split(","))

    selected_names = set()
    invalid = []

    for raw in tokens:
        token = raw.strip().lower().replace("-", "_")
        if not token:
            continue
        if token == "all":
            return None, []
        resolved = _PROJECT_DOC_ALIAS_TO_NAME.get(token)
        if resolved:
            selected_names.add(resolved)
        else:
            invalid.append(raw.strip())

    return (selected_names or None), invalid


class LLMFatalError(RuntimeError):
    """Raised when docs generation must abort due to LLM runtime failures."""

# Map: logical template key → filename inside _TEMPLATES_DIR
_TEMPLATE_FILES: Dict[str, str] = {
    "README":       "TEMPLATE_README.md",
    "API":          "TEMPLATE_API.md",
    "ARCHITECTURE": "TEMPLATE_ARCHITECTURE.md",
    "INSTALLATION": "TEMPLATE_INSTALLATION.md",
    "DATABASE":     "TEMPLATE_DATABASE.md",
    "REQUIREMENTS": "TEMPLATE_REQUIREMENTS.md",
    "USE_CASES":    "TEMPLATE_USE_CASES.md",
    "RELEASE_NOTES":"TEMPLATE_RELEASE_NOTES.md",
    "USER_MANUAL":  "TEMPLATE_USER_MANUAL.md",
    "API_USAGE":    "TEMPLATE_API_USAGE.md",
    "DATABASE_ER":  "TEMPLATE_DATABASE_ER.md",
    "INDEX":        "TEMPLATE_INDEX.md",
}


def _load_template(key: str) -> str:
    """Load a document structure template by its key.

    Returns the template Markdown content, or an empty string if the template
    file cannot be found (generation continues without template guidance).
    """
    filename = _TEMPLATE_FILES.get(key, "")
    if not filename:
        return ""
    if key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[key]
    path = _TEMPLATES_DIR / filename
    try:
        content = path.read_text(encoding="utf-8")
        _TEMPLATE_CACHE[key] = content
        return content
    except OSError:
        _TEMPLATE_CACHE[key] = ""
        return ""


def _template_digest() -> str:
    """Stable digest used to force regeneration when templates change."""
    chunks: List[str] = []
    for key in sorted(_TEMPLATE_FILES):
        chunks.append(f"{key}:{_load_template(key)}")
    joined = "\n".join(chunks)
    return hashlib.sha256(joined.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _template_key_for_doc_name(doc_name: str) -> str:
    mapping = {
        "README.md": "README",
        "API Documentation": "API",
        "Architecture": "ARCHITECTURE",
        "Installation Guide": "INSTALLATION",
        "Database Schema": "DATABASE",
        "Requirements (SRS)": "REQUIREMENTS",
        "Use Cases": "USE_CASES",
        "Release Notes": "RELEASE_NOTES",
        "User Manual": "USER_MANUAL",
        "API Usage Guide": "API_USAGE",
        "Database ER": "DATABASE_ER",
        "index.md": "INDEX",
    }
    return mapping.get(doc_name, "")


def _missing_template_reason(template_key: str) -> str:
    filename = _TEMPLATE_FILES.get(template_key, "(unknown)")
    return f"Missing required template: docs/templates/{filename}"


def _context_limit_for_model(model_name: str) -> int:
    """Adaptive context budget to keep 32B-class models responsive."""
    name = (model_name or "").lower()
    if "70b" in name:
        return 70000
    if "32b" in name or "34b" in name:
        return 90000
    if "14b" in name:
        return 120000
    return 180000


def _template_instruction(key: str) -> str:
    """Build the template-injection block to append to LLM prompts.

    The returned string instructs the LLM to follow the template structure
    while replacing all placeholder text with real project-derived content.
    Returns an empty string when no template is available.
    """
    content = _load_template(key)
    if not content:
        return ""
    return (
        "\n\n=== DOCUMENT STRUCTURE TEMPLATE ===\n"
        "You MUST follow the section headings and formatting layout shown in this template.\n"
        "Replace ALL placeholder text (e.g. [Project Name], YYYY-MM-DD, example.com) with\n"
        "real values derived from the project evidence provided above.\n"
        "Do NOT copy template placeholder sentences verbatim — write original content.\n"
        "Omit any section that has no evidenced content (e.g. omit Webhooks if none detected).\n\n"
        + content
        + "\n=== END TEMPLATE ===\n"
    )


class QwenLLM:
    """Interface to Qwen LLM via Ollama."""
    
    def __init__(self, model="qwen2.5-coder:7b", base_url="http://localhost:11434"):
        self.model = model
        self.base_url = base_url
        self.api_url = f"{base_url}/api/generate"
        self.tags_url = f"{base_url}/api/tags"
        self.call_count = 0
        self.request_timeout = int(os.getenv("FOXNEST_LLM_REQUEST_TIMEOUT_SECONDS", "240"))
        self.max_retries = int(os.getenv("FOXNEST_LLM_MAX_RETRIES", "1"))

        # Large models can stall for very long; keep calls bounded.
        lowered = (model or "").lower()
        if "32b" in lowered or "34b" in lowered or "70b" in lowered:
            self.request_timeout = min(self.request_timeout, 300)
            self.max_retries = min(self.max_retries, 1)

    @staticmethod
    def _is_likely_gpu_issue(text: str) -> bool:
        msg = (text or "").lower()
        gpu_tokens = [
            "cuda",
            "cudnn",
            "gpu",
            "rocm",
            "hip",
            "no suitable devices",
            "out of memory",
            "insufficient memory",
            "failed to initialize",
            "driver",
        ]
        return any(token in msg for token in gpu_tokens)

    @staticmethod
    def _extract_http_error_body(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        if response is None:
            return str(exc)
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("error") or payload.get("message") or payload)
            return str(payload)
        except Exception:
            try:
                return response.text[:600]
            except Exception:
                return str(exc)

    def validate_runtime(self) -> None:
        """Fail fast when Ollama, model, or runtime is unavailable."""
        try:
            tags_resp = requests.get(self.tags_url, timeout=min(20, self.request_timeout))
            tags_resp.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise LLMFatalError(
                "LLM service is unreachable at "
                f"{self.base_url}. Ensure Ollama is running (e.g. `ollama serve`)."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise LLMFatalError(
                "LLM service health check timed out. Ollama may be overloaded or unresponsive."
            ) from exc
        except Exception as exc:
            details = self._extract_http_error_body(exc)
            raise LLMFatalError(f"LLM service health check failed: {details}") from exc

        try:
            tags_payload = tags_resp.json() or {}
        except Exception:
            tags_payload = {}

        model_names = {
            str(m.get("name", "")).strip()
            for m in tags_payload.get("models", [])
            if isinstance(m, dict)
        }
        if self.model not in model_names:
            short = sorted([m for m in model_names if m])[:8]
            available = ", ".join(short) if short else "none"
            raise LLMFatalError(
                f"Requested model '{self.model}' is not available in Ollama. "
                f"Run `ollama pull {self.model}`. Available models: {available}"
            )

        # Tiny probe generation to catch runtime-level failures before long job execution.
        try:
            probe = requests.post(
                self.api_url,
                json={
                    "model": self.model,
                    "prompt": "ping",
                    "stream": False,
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 1,
                        "num_ctx": 2048,
                    },
                },
                timeout=min(45, self.request_timeout),
            )
            probe.raise_for_status()
        except Exception as exc:
            details = self._extract_http_error_body(exc)
            if self._is_likely_gpu_issue(details):
                raise LLMFatalError(
                    "GPU/runtime appears unavailable for this model. "
                    "Check GPU drivers/runtime, reduce model size, or run a CPU-capable model. "
                    f"Details: {details}"
                ) from exc
            raise LLMFatalError(f"LLM model probe failed: {details}") from exc
    
    def generate(self, prompt: str, max_tokens: int = 2000, temperature: float = 0.15) -> str:
        """Generate text from Qwen model with automatic retry on timeout/error."""
        self.call_count += 1
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    self.api_url,
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                            "num_ctx": 65536,
                            "top_p": 0.9,
                            "repeat_penalty": 1.05,
                        }
                    },
                    timeout=self.request_timeout
                )
                response.raise_for_status()
                return response.json().get("response", "").strip()
            except requests.exceptions.ConnectionError as e:
                raise LLMFatalError(
                    f"Lost connection to LLM service at {self.base_url} during generation."
                ) from e
            except requests.exceptions.Timeout:
                last_error = f"Timeout on attempt {attempt + 1}"
                continue
            except requests.exceptions.HTTPError as e:
                details = self._extract_http_error_body(e)
                if self._is_likely_gpu_issue(details):
                    raise LLMFatalError(
                        "GPU/runtime failure while generating documentation. "
                        f"Details: {details}"
                    ) from e
                last_error = details
            except Exception as e:
                last_error = str(e)
                break
        return f"[LLM Error: {last_error}]"


def analyze_project_structure(project_path: str) -> Dict[str, Any]:
    """Analyze project structure and extract key information."""
    project_path = Path(project_path)
    
    info = {
        "name": project_path.name,
        "languages": set(),
        "frameworks": set(),
        "dependencies": {},
        "import_signals": set(),
        "entry_points": [],
        "config_files": [],
        "test_files": [],
        "docs_files": [],
        "folders": [],
        "file_count": 0,
        "line_count": 0,
    }
    
    # Scan files
    for root, dirs, files in os.walk(project_path):
        # Skip common ignore dirs and third-party/vendor caches
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        
        rel_root = Path(root).relative_to(project_path)
        if rel_root != Path('.'):
            info["folders"].append(str(rel_root))
        
        for file in files:
            file_path = Path(root) / file
            info["file_count"] += 1
            
            # Detect languages
            ext = file_path.suffix.lower()
            if ext == '.py':
                info["languages"].add("Python")
            elif ext in {'.js', '.jsx'}:
                info["languages"].add("JavaScript")
            elif ext in {'.ts', '.tsx'}:
                info["languages"].add("TypeScript")
            elif ext == '.java':
                info["languages"].add("Java")
            elif ext in {'.kt', '.kts'}:
                info["languages"].add("Kotlin")
            elif ext in {'.cpp', '.cc', '.cxx', '.h', '.hpp'}:
                info["languages"].add("C++")
            elif ext in {'.c'}:
                info["languages"].add("C")
            elif ext == '.go':
                info["languages"].add("Go")
            elif ext == '.rs':
                info["languages"].add("Rust")
            elif ext == '.php':
                info["languages"].add("PHP")
            elif ext == '.rb':
                info["languages"].add("Ruby")
            elif ext == '.cs':
                info["languages"].add("C#")
            elif ext == '.swift':
                info["languages"].add("Swift")
            elif ext == '.dart':
                info["languages"].add("Dart")
            elif ext == '.lua':
                info["languages"].add("Lua")
            elif ext in {'.m', '.mm'}:
                info["languages"].add("Objective-C")
            
            # Detect config/special files
            if file in {'requirements.txt', 'setup.py', 'pyproject.toml', 'Pipfile'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file in {'package.json', 'package-lock.json'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file in {'composer.json', 'composer.lock'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file in {'Gemfile', 'Gemfile.lock'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file in {'pubspec.yaml', 'pubspec.lock', 'go.mod', 'go.sum', 'Cargo.toml', 'Cargo.lock'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file in {'pom.xml', 'build.gradle', 'settings.gradle'}:
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file == 'Dockerfile':
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif file.endswith('.service'):
                info["config_files"].append(str(file_path.relative_to(project_path)))
            elif 'test' in file.lower() or file.startswith('test_'):
                info["test_files"].append(str(file_path.relative_to(project_path)))
            elif file.lower() in {'readme.md', 'readme.txt', 'readme'}:
                info["docs_files"].append(str(file_path.relative_to(project_path)))
            
            # Entry points
            if file in {'main.py', 'app.py', 'server.py', 'index.js', 'main.js', 'index.ts',
                        'index.php', 'app.php', 'public/index.php', 'artisan', 'bootstrap/app.php',
                        'main.rb', 'app.rb', 'config.ru', 'main.go', 'main.dart', 'lib/main.dart',
                        'Program.cs', 'Startup.cs', 'main.swift', 'main.lua'}:
                info["entry_points"].append(str(file_path.relative_to(project_path)))
            
            # Count lines for ALL source languages
            line_exts = {
                '.py', '.js', '.jsx', '.ts', '.tsx',
                '.java', '.kt', '.kts',
                '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp',
                '.go', '.rs',
                '.php', '.rb', '.cs', '.swift', '.dart', '.lua',
                '.m', '.mm',
            }
            if ext in line_exts:
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        info["line_count"] += len(content.splitlines())

                        if ext == '.py':
                            import_matches = re.findall(r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", content, flags=re.MULTILINE)
                            for frm, imp in import_matches:
                                module = (frm or imp or "").split('.')[0].strip()
                                if module:
                                    info["import_signals"].add(module)
                            if '__main__' in content and str(file_path.relative_to(project_path)) not in info["entry_points"]:
                                info["entry_points"].append(str(file_path.relative_to(project_path)))

                        elif ext == '.php':
                            # PHP: detect use / require / namespace signals
                            php_uses = re.findall(r'^\s*use\s+([\\\w]+)', content, flags=re.MULTILINE)
                            for u in php_uses:
                                top = u.strip('\\').split('\\')[0].strip()
                                if top:
                                    info["import_signals"].add(top)
                            php_req = re.findall(r"require(?:_once)?\s*[\(\s]['\"]([^'\"]+)['\"]", content)
                            for r in php_req:
                                info["import_signals"].add(Path(r).stem[:40])

                        elif ext == '.rb':
                            rb_req = re.findall(r"^\s*require(?:_relative)?\s*['\"]([^'\"]+)['\"]", content, flags=re.MULTILINE)
                            for r in rb_req:
                                info["import_signals"].add(Path(r).stem[:40])

                        elif ext in {'.dart'}:
                            dart_imp = re.findall(r"^\s*import\s*['\"]([^'\"]+)['\"]", content, flags=re.MULTILINE)
                            for d in dart_imp:
                                pkg = d.split('/')[0].replace('package:', '').strip()
                                if pkg:
                                    info["import_signals"].add(pkg)

                except Exception:
                    pass
    
    # Parse dependencies
    if (project_path / 'requirements.txt').exists():
        try:
            with open(project_path / 'requirements.txt', 'r') as f:
                deps = [line.split('==')[0].split('>=')[0].strip() for line in f if line.strip() and not line.startswith('#')]
                info["dependencies"]["python"] = deps[:20]
        except:
            pass
    
    if (project_path / 'composer.json').exists():
        try:
            with open(project_path / 'composer.json', 'r') as f:
                pkg = json.load(f)
                deps = list(pkg.get('require', {}).keys())
                dev_deps = list(pkg.get('require-dev', {}).keys())
                info["dependencies"]["php"] = [d for d in deps + dev_deps if not d.startswith('php') and '/' in d][:25]
                # Detect PHP frameworks
                dep_str = ' '.join(deps + dev_deps).lower()
                if 'laravel/framework' in dep_str:
                    info["frameworks"].add("Laravel")
                if 'symfony/' in dep_str:
                    info["frameworks"].add("Symfony")
                if 'slim/slim' in dep_str:
                    info["frameworks"].add("Slim")
                if 'yiisoft/yii2' in dep_str or 'yii2' in dep_str:
                    info["frameworks"].add("Yii2")
                if 'cakephp/' in dep_str:
                    info["frameworks"].add("CakePHP")
                if 'codeigniter' in dep_str:
                    info["frameworks"].add("CodeIgniter")
                if 'doctrine/' in dep_str:
                    info["frameworks"].add("Doctrine ORM")
                if 'illuminate/database' in dep_str:
                    info["frameworks"].add("Eloquent ORM")
                # Entry points
                scripts = pkg.get('scripts', {})
                if 'artisan' in str(scripts).lower():
                    info["frameworks"].add("Laravel")
        except:
            pass

    if (project_path / 'Gemfile').exists():
        try:
            with open(project_path / 'Gemfile', 'r') as f:
                content = f.read()
                gems = re.findall(r"^\s*gem\s*['\"]([^'\"]+)['\"]", content, flags=re.MULTILINE)
                info["dependencies"]["ruby"] = gems[:25]
                gem_str = content.lower()
                if 'rails' in gem_str:
                    info["frameworks"].add("Rails")
                if 'sinatra' in gem_str:
                    info["frameworks"].add("Sinatra")
        except:
            pass
    
    if (project_path / 'package.json').exists():
        try:
            with open(project_path / 'package.json', 'r') as f:
                pkg = json.load(f)
                deps = list(pkg.get('dependencies', {}).keys())[:20]
                info["dependencies"]["javascript"] = deps
                
                # Detect frameworks
                if 'react' in deps:
                    info["frameworks"].add("React")
                if 'vue' in deps:
                    info["frameworks"].add("Vue")
                if 'express' in deps:
                    info["frameworks"].add("Express")
                if 'fastify' in deps:
                    info["frameworks"].add("Fastify")
        except:
            pass
    
    # Detect Python frameworks
    if "Python" in info["languages"]:
        dep_str = str(info["dependencies"].get("python", []))
        import_str = str(sorted(info.get("import_signals", [])))
        if 'fastapi' in dep_str.lower():
            info["frameworks"].add("FastAPI")
        if 'fastapi' in import_str.lower():
            info["frameworks"].add("FastAPI")
        if 'flask' in dep_str.lower():
            info["frameworks"].add("Flask")
        if 'flask' in import_str.lower():
            info["frameworks"].add("Flask")
        if 'django' in dep_str.lower():
            info["frameworks"].add("Django")
        if 'django' in import_str.lower():
            info["frameworks"].add("Django")
        if 'sqlalchemy' in dep_str.lower():
            info["frameworks"].add("SQLAlchemy")
        if 'sqlalchemy' in import_str.lower():
            info["frameworks"].add("SQLAlchemy")

        if any(x in import_str.lower() for x in ['pyqt5', 'pyqt6', 'pyside2', 'pyside6']):
            info["frameworks"].add("PyQt/PySide")
        if 'tkinter' in import_str.lower() or 'customtkinter' in import_str.lower():
            info["frameworks"].add("Tkinter")
        if 'kivy' in import_str.lower():
            info["frameworks"].add("Kivy")
        if 'flet' in import_str.lower():
            info["frameworks"].add("Flet")

        # If requirements are missing, expose import-based dependencies (best-effort)
        if not info["dependencies"].get("python") and info.get("import_signals"):
            stdlib_like = {
                'os', 'sys', 're', 'json', 'math', 'time', 'datetime', 'pathlib', 'hashlib', 'subprocess',
                'collections', 'itertools', 'functools', 'typing', 'logging', 'argparse', 'tempfile',
                'shutil', 'zipfile', 'base64', 'csv', 'threading', 'asyncio', 'struct', 'mimetypes',
                'difflib', 'unittest', 'http', 'urllib', 'socket', 'traceback', 'platform'
            }
            inferred = [m for m in sorted(info["import_signals"]) if m.lower() not in stdlib_like]
            if inferred:
                info["dependencies"]["python"] = inferred[:25]
    
    # Extract environment variable names from os.getenv() / os.environ.get() calls
    env_vars = set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        for file in files:
            if not file.endswith('.py'):
                continue
            try:
                with open(Path(root) / file, 'r', encoding='utf-8', errors='ignore') as f:
                    src = f.read()
                for match in re.finditer(r'os\.(?:getenv|environ\.get)\([\'"]([A-Z][A-Z0-9_]{2,})[\'"]', src):
                    env_vars.add(match.group(1))
            except Exception:
                pass
    info["env_vars"] = sorted(env_vars)

    # Normalize non-JSON-safe sets
    info["import_signals"] = sorted(info.get("import_signals", []))
    return info


def _is_source_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
        ".kt", ".kts", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
        ".php", ".rb", ".swift", ".sql", ".sh", ".md", ".json", ".toml", ".yaml", ".yml"
    }


def _representative_excerpt(content: str, max_chars: int = 6000) -> str:
    """Build an excerpt that samples beginning/middle/end and symbol-heavy regions."""
    if not content:
        return ""
    if len(content) <= max_chars:
        return content

    chunks = []
    used = set()

    def add_chunk(start: int, length: int):
        start = max(0, min(start, len(content) - 1))
        end = min(len(content), start + length)
        key = (start, end)
        if key in used or start >= end:
            return
        used.add(key)
        chunks.append(content[start:end].strip())

    head_len = int(max_chars * 0.35)
    tail_len = int(max_chars * 0.20)
    mid_len = int(max_chars * 0.20)
    signal_len = max_chars - (head_len + tail_len + mid_len)

    add_chunk(0, head_len)
    add_chunk(max(0, (len(content) // 2) - (mid_len // 2)), mid_len)
    add_chunk(max(0, len(content) - tail_len), tail_len)

    # Add chunks around key structural markers (class/def/main/UI)
    markers = [
        r"\nclass\s+\w+",
        r"\ndef\s+\w+\s*\(",
        r"if\s+__name__\s*==\s*['\"]__main__['\"]",
        r"QMainWindow|QWidget|QApplication|Tk\(|customtkinter|PyQt",
    ]
    per_signal = max(180, signal_len // 3) if signal_len > 0 else 0
    for pattern in markers:
        if signal_len <= 0:
            break
        match = re.search(pattern, content)
        if not match:
            continue
        start = max(0, match.start() - (per_signal // 3))
        add_chunk(start, per_signal)
        signal_len -= per_signal

    return "\n\n# --- excerpt break ---\n\n".join([c for c in chunks if c])[:max_chars]


def _extract_structural_signals(rel_path: str, content: str) -> str:
    """Summarize concrete implementation signals from full file content."""
    if not content:
        return f"- {rel_path}: empty or unreadable"

    imports = re.findall(r"^\s*(?:from\s+[^\n]+\s+import\s+[^\n]+|import\s+[^\n]+)", content, flags=re.MULTILINE)
    classes = re.findall(r"^\s*class\s+(\w+)", content, flags=re.MULTILINE)
    functions = re.findall(r"^\s*def\s+(\w+)\s*\(", content, flags=re.MULTILINE)

    ui_hits = []
    for token in ["QMainWindow", "QWidget", "QApplication", "QTabWidget", "QPushButton", "Tk(", "customtkinter", "PyQt", "PySide"]:
        if token in content:
            ui_hits.append(token)

    imports_short = [i.strip() for i in imports[:8]]
    classes_short = classes[:12]
    functions_short = functions[:18]

    literal_candidates = re.findall(r"['\"]([A-Za-z][A-Za-z0-9 _\-/]{2,48})['\"]", content)
    ui_labels = []
    for literal in literal_candidates:
        low = literal.lower()
        if any(k in low for k in [
            "dashboard", "analysis", "tool", "debug", "import", "section", "header",
            "timeline", "signature", "report", "upload", "save", "run", "hex", "converter"
        ]):
            ui_labels.append(literal.strip())
    ui_labels = list(dict.fromkeys(ui_labels))[:20]

    parts = [f"- File: {rel_path}"]
    parts.append(f"  - Imports sample: {', '.join(imports_short) if imports_short else 'none'}")
    parts.append(f"  - Classes ({len(classes)}): {', '.join(classes_short) if classes_short else 'none'}")
    parts.append(f"  - Functions ({len(functions)}): {', '.join(functions_short) if functions_short else 'none'}")
    if ui_hits:
        parts.append(f"  - UI/Desktop signals: {', '.join(ui_hits)}")
    if ui_labels:
        parts.append(f"  - UI labels/text (explicit): {', '.join(ui_labels)}")

    return "\n".join(parts)


def collect_code_context(project_path: str, project_info: Dict[str, Any], endpoints: List[Dict[str, Any]], models: List[Dict[str, Any]], max_files: int = 50, max_chars: int = 500000) -> str:
    """Collect concrete code excerpts to ground LLM prompts in real implementation details."""
    project_path = Path(project_path)

    ranked_files = []
    seen = set()

    preferred_names = [
        "README.md", "server.py", "main.py", "app.py", "package.json",
        "requirements.txt", "pyproject.toml", "docker-compose.yml", "Dockerfile",
        ".env.example", ".env.sample", "config.py", "settings.py",
    ]

    # Prefer known project-defining files first
    for name in preferred_names:
        candidate = project_path / name
        if candidate.exists() and candidate.is_file():
            rel = str(candidate.relative_to(project_path))
            ranked_files.append(rel)
            seen.add(rel)

    # Also include .service / setup shell scripts for deployment context
    for file_path in sorted(project_path.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix in {".service", ".sh"} or file_path.name in {"setup.sh", "start.sh", "run.sh"}:
            rel = str(file_path.relative_to(project_path))
            if rel not in seen:
                ranked_files.append(rel)
                seen.add(rel)

    # Add entry points and config files discovered by analysis
    for rel in project_info.get("entry_points", []) + project_info.get("config_files", []):
        if rel not in seen:
            ranked_files.append(rel)
            seen.add(rel)

    # Add files that define detected endpoints/models
    for ep in endpoints:
        rel = ep.get("file")
        if rel and rel not in seen:
            ranked_files.append(rel)
            seen.add(rel)
    for model in models:
        rel = model.get("file")
        if rel and rel not in seen:
            ranked_files.append(rel)
            seen.add(rel)

    # Fill remaining with source files from tree (stable order)
    for file_path in sorted(project_path.rglob("*")):
        if len(ranked_files) >= max_files * 2:
            break
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(project_path))
        if rel in seen:
            continue
        if _has_excluded_part(file_path):
            continue
        if _is_source_file(file_path):
            ranked_files.append(rel)
            seen.add(rel)

    # Expand limits for small/single-file repositories so more of real code is considered
    source_candidates = []
    for file_path in sorted(project_path.rglob("*")):
        if not file_path.is_file():
            continue
        if _has_excluded_part(file_path):
            continue
        if _is_source_file(file_path):
            source_candidates.append(file_path)

    total_sources = len(source_candidates)
    if total_sources <= 3:
        max_files = max(max_files, 50)
        max_chars = max(max_chars, 1000000)
    elif total_sources <= 12:
        max_files = max(max_files, 50)
        max_chars = max(max_chars, 700000)

    context_parts = []
    signal_parts = []
    used_chars = 0
    included = 0

    for rel in ranked_files:
        if included >= max_files or used_chars >= max_chars:
            break
        full_path = project_path / rel
        if not full_path.exists() or not full_path.is_file():
            continue
        if not _is_source_file(full_path):
            continue

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue

        if not content.strip():
            continue

        # Send the FULL file content so the model reads all logic, not just a sample.
        # A per-file hard cap prevents one giant file from consuming the entire budget.
        PER_FILE_CAP = 120000
        full_content = content
        if len(content) > PER_FILE_CAP:
            full_content = (
                content[:PER_FILE_CAP]
                + f"\n\n# ... ({len(content) - PER_FILE_CAP} chars omitted — file exceeds per-file cap) ..."
            )
        block = f"### File: {rel}\n```\n{full_content}\n```\n"
        block_len = len(block)

        if used_chars + block_len > max_chars:
            remaining = max_chars - used_chars
            if remaining < 500:
                break
            # Partial include: send as much of the file as the remaining budget allows
            partial = content[:max(300, remaining - 150)]
            block = f"### File: {rel}\n```\n{partial}\n```\n"
            block_len = len(block)

        context_parts.append(block)
        signal_parts.append(_extract_structural_signals(rel, content))
        used_chars += block_len
        included += 1

    if not context_parts:
        return "No code excerpts available."

    preface = (
        "Use the following real project excerpts as authoritative implementation context. "
        "Do not invent components not evidenced below. If uncertain, explicitly say it is unknown.\n\n"
    )

    signals_section = "## Repository-wide Structural Signals\n\n" + "\n\n".join(signal_parts[:max_files])
    excerpts_section = "## Representative Source Excerpts\n\n" + "\n".join(context_parts)
    project_info["structural_signals"] = signal_parts[:max_files]
    return preface + signals_section + "\n\n" + excerpts_section


def _scan_existing_docs(project_path: Path) -> str:
    """Read existing documentation files to extract the team's established style,
    terminology, section names, and formatting preferences.

    The result is prepended to every LLM prompt so the generated documentation
    matches what the team has already written manually.
    """
    candidates = [
        project_path / "README.md",
        project_path / "CHANGELOG.md",
        project_path / "CONTRIBUTING.md",
        project_path / "docs" / "README.md",
        project_path / "docs" / "index.md",
        project_path / "docs" / "CONTRIBUTING.md",
    ]
    found = []
    total = 0
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            excerpt = content[:2500].strip()
            if not excerpt:
                continue
            rel = path.relative_to(project_path)
            found.append(f"=== Existing: {rel} ===\n{excerpt}")
            total += len(excerpt)
            if total >= 8000:
                break
        except Exception:
            pass
    if not found:
        return ""
    return (
        "## Existing Documentation Style Reference\n"
        "The files below were written by this team. Study their section names, tone,\n"
        "terminology, and formatting style. Apply the SAME conventions in your output.\n\n"
        + "\n\n".join(found)
        + "\n\n--- end of style reference ---"
    )


def _with_code_context(prompt: str, code_context: str, max_context_chars: Optional[int] = None) -> str:
    """Append source code as read-only reference to a prompt.

    The injected block includes explicit instructions that the LLM must NOT
    reproduce the code in its output — it should only use it as evidence to
    understand the project and write accurate prose documentation.
    """
    if not code_context:
        return prompt
    if max_context_chars is None:
        max_context_chars = _CODE_CONTEXT_CHAR_LIMIT
    # Trim to avoid overflowing the model's context window and causing echo
    trimmed = code_context[:max_context_chars]
    if len(code_context) > max_context_chars:
        trimmed += "\n... [source truncated for brevity] ..."
    return (
        f"{prompt}\n\n"
        "=== SOURCE CODE REFERENCE (DO NOT REPRODUCE) ===\n"
        "IMPORTANT: The code below is provided ONLY as evidence to help you understand the project.\n"
        "- Do NOT copy, paste, or reproduce any source code in your output.\n"
        "- Do NOT include raw function bodies, class definitions, or import lists in the document.\n"
        "- Use the code purely to extract facts: names, purposes, parameters, behaviours.\n"
        "- Your output must be human-readable prose, tables, numbered steps, and short inline snippets (≤3 lines) only.\n"
        "=================================================\n"
        f"{trimmed}\n"
        "=================================================\n"
        "Remember: write documentation, NOT code. Do not echo the source above."
    )


def _build_evidence_summary(project_info: Dict[str, Any], endpoints: List[Dict[str, Any]], models: List[Dict[str, Any]]) -> str:
    deps = project_info.get("dependencies", {}) or {}
    py_deps = deps.get("python", [])
    js_deps = deps.get("javascript", [])
    all_deps = [str(d).lower() for d in (py_deps + js_deps)]

    app_signals = []
    if any(dep in all_deps for dep in ["pyqt5", "pyqt6", "pyside2", "pyside6", "tkinter", "customtkinter", "kivy", "flet"]):
        app_signals.append("Desktop UI application signals detected")
    if any(dep in all_deps for dep in ["electron", "tauri"]):
        app_signals.append("Desktop shell/runtime signals detected")
    if endpoints:
        app_signals.append("Web/API backend signals detected (FastAPI/HTTP routes)")

    summary = [
        f"Project name: {project_info.get('name', 'unknown')}",
        f"Languages: {', '.join(project_info.get('languages', [])) or 'None detected'}",
        f"Frameworks: {', '.join(project_info.get('frameworks', [])) or 'None detected'}",
        f"Import signals (sample): {', '.join((project_info.get('import_signals') or [])[:30]) or 'None detected'}",
        f"Entry points: {', '.join(project_info.get('entry_points', [])) or 'None detected'}",
        f"Config files: {', '.join(project_info.get('config_files', [])) or 'None detected'}",
        f"Detected API endpoints: {len(endpoints)}",
        f"Detected DB models: {len(models)}",
        f"Python dependencies (sample): {', '.join(py_deps[:20]) or 'None'}",
        f"JavaScript dependencies (sample): {', '.join(js_deps[:20]) or 'None'}",
        f"Project folders (sample): {', '.join(project_info.get('folders', [])[:20]) or 'None detected'}",
    ]

    if app_signals:
        summary.append(f"Application type signals: {'; '.join(app_signals)}")
    else:
        summary.append("Application type signals: No strong type signal detected")

    summary.append(
        "Strict rule: Mention only technologies/components evidenced above or explicitly present in code excerpts. "
        "If unknown, write 'Not detected from repository evidence'."
    )

    return "\n".join(f"- {item}" for item in summary)


def _is_llm_error(text: str) -> bool:
    return bool(text and text.strip().startswith("[LLM Error:"))


def _extract_ui_labels_from_signals(signals: List[str]) -> List[str]:
    labels = []
    for signal in signals or []:
        match = re.search(r"UI labels/text \(explicit\):\s*(.+)", signal)
        if not match:
            continue
        for item in match.group(1).split(","):
            cleaned = item.strip()
            if cleaned and cleaned not in labels:
                labels.append(cleaned)
    return labels[:20]


def _build_deterministic_readme(project_info: Dict[str, Any], endpoints: List[Dict], models: List[Dict]) -> str:
    languages = ", ".join(project_info.get("languages", [])) or "Not detected"
    frameworks = ", ".join(project_info.get("frameworks", [])) or "Not detected"
    entry_points = project_info.get("entry_points", [])
    deps = project_info.get("dependencies", {}) or {}
    py_deps = deps.get("python", [])[:20]
    ui_labels = _extract_ui_labels_from_signals(project_info.get("structural_signals", []))

    feature_lines = []
    if ui_labels:
        feature_lines.append("- UI modules detected from code labels: " + ", ".join(ui_labels[:10]))
    if "PyQt5" in " ".join(py_deps) or "pyqt5" in " ".join(py_deps).lower():
        feature_lines.append("- Desktop GUI implementation via PyQt5 is explicitly present in dependencies/imports")
    if endpoints:
        feature_lines.append(f"- API endpoints detected: {len(endpoints)}")
    if models:
        feature_lines.append(f"- Database models detected: {len(models)}")
    if not feature_lines:
        feature_lines.append("- Core capabilities are inferred from class/function structure in source files")

    structure_lines = [
        f"- Total files analyzed: {project_info.get('file_count', 0)}",
        f"- Total code lines: {project_info.get('line_count', 0)}",
    ]
    if entry_points:
        structure_lines.append("- Entry points: " + ", ".join(entry_points[:8]))

    install_deps = " ".join(py_deps[:18]) if py_deps else "-r requirements.txt"
    usage_cmd = f"python {entry_points[0]}" if entry_points else "python <entry-file>.py"

    readme = [
        f"# {project_info.get('name', 'Project')}",
        "",
        "## Overview",
        "This documentation is generated from repository evidence (imports, classes, functions, and UI labels).",
        "",
        "## Features",
        *feature_lines,
        "",
        "## Tech Stack",
        f"- Languages: {languages}",
        f"- Frameworks: {frameworks}",
        f"- Python dependencies (sample): {', '.join(py_deps) if py_deps else 'Not detected'}",
        "",
        "## Project Structure",
        *structure_lines,
        "",
        "## Installation",
        "1. Clone the repository",
        "2. Create and activate a virtual environment",
        f"3. Install dependencies: `pip install {install_deps}`",
        "",
        "## Usage",
        f"Run: `{usage_cmd}`",
        "",
        "## API Endpoints",
        f"Detected endpoints: {len(endpoints)}",
        "",
        "## Database Schema",
        f"Detected models: {len(models)}",
    ]
    return "\n".join(readme)


def _build_deterministic_architecture(project_info: Dict[str, Any], endpoints: List[Dict], models: List[Dict]) -> str:
    languages = ", ".join(project_info.get("languages", [])) or "Not detected from repository evidence"
    frameworks = ", ".join(project_info.get("frameworks", [])) or "Not detected from repository evidence"
    entry_points = ", ".join(project_info.get("entry_points", [])[:8]) or "Not detected from repository evidence"
    ui_labels = _extract_ui_labels_from_signals(project_info.get("structural_signals", []))

    doc = [
        "# System Overview",
        "",
        "## High-level description",
        "Architecture is derived from repository evidence (source structure, symbols, and UI labels).",
        "",
        "## Architecture Pattern",
        "- Pattern: Modular desktop application (evidence-based)",
        "- Reason: concentrated application logic in entry-point Python module(s) with GUI/UI signals",
        "",
        "## Component Diagram (text description)",
        "- UI Layer: user-facing screens/panels inferred from explicit labels",
        "- Core Logic Layer: functions/classes handling analysis and processing workflows",
        "- Data/IO Layer: file parsing and result generation modules",
    ]
    if ui_labels:
        doc.extend(["", "- UI labels observed: " + ", ".join(ui_labels[:12])])

    doc.extend([
        "",
        "## Technology Stack",
        f"- Languages: {languages}",
        f"- Frameworks: {frameworks}",
        "",
        "## Data Flow",
        "1. User loads input file(s) in the desktop UI",
        "2. Core analysis routines process content",
        "3. Results are rendered back to UI sections/panels",
        "",
        "## Security Architecture",
        "- Authentication/authorization: Not detected from repository evidence",
        "- Data protection measures: Not detected from repository evidence",
        "",
        f"## Evidence Summary\n- Entry points: {entry_points}\n- Endpoints detected: {len(endpoints)}\n- Models detected: {len(models)}",
    ])
    return "\n".join(doc)


def extract_fastapi_endpoints(project_path: str) -> List[Dict[str, Any]]:
    """Extract FastAPI endpoints from Python files."""
    endpoints = []
    
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in {'.git', 'venv', '__pycache__'}]
        
        for file in files:
            if not file.endswith('.py'):
                continue
            
            file_path = Path(root) / file
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                # Match FastAPI route decorators
                pattern = r'@(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']\s*[^)]*\)\s*(?:async\s+)?def\s+(\w+)'
                
                for match in re.finditer(pattern, content):
                    method = match.group(1).upper()
                    path = match.group(2)
                    func_name = match.group(3)
                    
                    # Try to extract docstring
                    func_start = match.end()
                    doc_match = re.search(r'^\s*"""([^"]+)"""', content[func_start:], re.MULTILINE)
                    docstring = doc_match.group(1).strip() if doc_match else ""
                    
                    endpoints.append({
                        "method": method,
                        "path": path,
                        "function": func_name,
                        "file": str(Path(file_path).relative_to(project_path)),
                        "docstring": docstring
                    })
            except:
                pass
    
    return endpoints


def extract_database_models(project_path: str) -> List[Dict[str, Any]]:
    """Extract SQLAlchemy database models."""
    models = []
    
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in {'.git', 'venv', '__pycache__'}]
        
        for file in files:
            if not file.endswith('.py'):
                continue
            
            file_path = Path(root) / file
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                # Match SQLAlchemy models
                pattern = r'class\s+(\w+)\s*\([^)]*Base[^)]*\):\s*(?:"""([^"]+)""")?'
                
                for match in re.finditer(pattern, content):
                    class_name = match.group(1)
                    docstring = match.group(2).strip() if match.group(2) else ""
                    
                    # Extract columns
                    class_start = match.end()
                    next_class = re.search(r'\nclass\s+', content[class_start:])
                    class_body = content[class_start:class_start + (next_class.start() if next_class else 500)]
                    
                    columns = []
                    col_pattern = r'(\w+)\s*=\s*Column\(([^)]+)\)'
                    for col_match in re.finditer(col_pattern, class_body):
                        columns.append({
                            "name": col_match.group(1),
                            "definition": col_match.group(2).strip()
                        })
                    
                    models.append({
                        "name": class_name,
                        "docstring": docstring,
                        "columns": columns,
                        "file": str(Path(file_path).relative_to(project_path))
                    })
            except:
                pass
    
    return models


def generate_readme(llm: QwenLLM, project_info: Dict, endpoints: List, models: List) -> str:
    """Generate comprehensive README.md using Qwen."""

    evidence_summary = _build_evidence_summary(project_info, endpoints, models)
    
    # Build concise endpoint table for the prompt
    ep_table = ""
    if endpoints:
        ep_table = "\n".join(
            f"  {e['method']:6} {e['path']:50} -> {e['function']}()"
            for e in endpoints[:30]
        )
    
    # Build model summary
    model_summary = ""
    if models:
        model_summary = "\n".join(
            f"  {m['name']}: " + ", ".join(c['name'] for c in m['columns'][:8])
            for m in models[:15]
        )

    template_block = _template_instruction("README")

    prompt = f"""You are a senior technical writer producing a production-ready README.md.
Every claim you make MUST be traceable to the evidence provided below.
Do NOT invent features, frameworks, services, or capabilities.

=== PROJECT EVIDENCE ===
{evidence_summary}

=== DETECTED API ENDPOINTS ({len(endpoints)}) ===
{ep_table if ep_table else 'None detected'}

=== DATABASE MODELS ({len(models)}) ===
{model_summary if model_summary else 'None detected'}
========================

{template_block}

Formatting rules:
- Use backticks for all symbol names, file paths, and commands.
- Use tables where data is tabular.
- Never write placeholder text like "[Your Name]" or "TODO".
- Target length: 400–700 lines of Markdown.
- Every claim MUST cite a real symbol, file, or endpoint from the evidence above."""
    readme = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=6000, temperature=0.1)

    if _is_llm_error(readme):
        frameworks = ", ".join(project_info.get("frameworks", [])) or "Not detected"
        languages = ", ".join(project_info.get("languages", [])) or "Not detected"
        entry_points = "\n".join([f"- `{ep}`" for ep in project_info.get("entry_points", [])[:12]]) or "- Not detected"
        readme = (
            f"# {project_info.get('name', 'Project')}\n\n"
            f"## Overview\n"
            f"This README is generated from repository evidence.\n\n"
            f"## Tech Stack\n"
            f"- Languages: {languages}\n"
            f"- Frameworks: {frameworks}\n"
            f"- API Endpoints Detected: {len(endpoints)}\n"
            f"- Database Models Detected: {len(models)}\n\n"
            f"## Entry Points\n{entry_points}\n"
        )
    
    # Add metadata
    header = (
        f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n"
        f"<!-- Model: {llm.model} -->\n\n"
    )
    
    return header + readme


def generate_api_docs(llm: QwenLLM, endpoints: List[Dict], project_info: Dict = None) -> Optional[str]:
    """Generate detailed API documentation."""
    
    if not endpoints:
        stub = (
            "# API Documentation\n\n"
            f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            "No FastAPI/HTTP endpoints were detected in this repository.\n\n"
            "If this project exposes a REST API, ensure routes are defined with "
            "`@app.get`, `@app.post`, etc. and re-run `fox generate-docs`.\n"
        )
        return stub
    
    doc = "# API Documentation\n\n"
    doc += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    doc += f"**Total Endpoints:** {len(endpoints)}\n\n"
    
    # Base URL and auth overview from code context
    api_template_block = _template_instruction("API")
    base_url_prompt = f"""Based on this project evidence, write an API Overview section covering:
1. Base URL pattern (infer from detected routes: {', '.join(e['path'] for e in endpoints[:5])})
2. Authentication method (look for Bearer/JWT/API key patterns in code)
3. Common response format (JSON expected based on FastAPI)
4. How to handle errors (HTTP status codes)

Only write what is directly supported by the evidence. Start with '## API Overview'.

{api_template_block}"""
    overview = llm.generate(
        _with_code_context(base_url_prompt, (project_info or {}).get("code_context", "")),
        max_tokens=600, temperature=0.2
    )
    doc += overview + "\n\n---\n\n"
    
    # Group by path prefix
    grouped = {}
    for ep in endpoints:
        prefix = ep['path'].split('/')[2] if ep['path'].count('/') >= 2 else ep['path'].split('/')[1] if '/' in ep['path'] else 'root'
        if prefix not in grouped:
            grouped[prefix] = []
        grouped[prefix].append(ep)
    
    for prefix, eps in sorted(grouped.items()):
        doc += f"## /{prefix}\n\n"
        
        for ep in eps:
            doc += f"### `{ep['method']} {ep['path']}`\n\n"
            doc += f"**Handler:** `{ep['function']}()`  \n"
            doc += f"**Source:** `{ep['file']}`\n\n"

            # Mark internal/admin endpoints
            _ep_internal = (
                '_internal' in ep['path']
                or ep['path'].endswith('/internal')
                or '/admin/' in ep['path']
                or ep['function'].startswith('_')
            )
            _ep_admin_only = '/admin' in ep['path'] or 'admin' in ep['function'].lower()
            if _ep_internal:
                doc += "> 🔒 **Internal Endpoint** — not intended for external API consumers. Subject to change.\n\n"
            elif _ep_admin_only:
                doc += "> 🛡️ **Admin Endpoint** — requires administrative privileges.\n\n"
            
            if ep['docstring']:
                doc += f"{ep['docstring']}\n\n"
            else:
                # Generate description with LLM, strongly anchored to handler name
                prompt = f"""Write a precise 2-3 sentence description for this REST endpoint.
Do NOT invent parameters or behaviour not implied by the handler name and path.

Method: {ep['method']}
Path: {ep['path']}
Handler function: {ep['function']}

Describe: what it does, who calls it, what it returns on success."""
                description = llm.generate(
                    _with_code_context(prompt, (project_info or {}).get("code_context", "")),
                    max_tokens=800, temperature=0.15
                )
                doc += f"{description}\n\n"
            
            # Generate request / response details
            detail_prompt = f"""Document this API endpoint with request/response details.
Only include parameters and response fields that are directly implied by the path template,
HTTP method semantics, and handler name. Do NOT invent fields not in evidence.

{ep['method']} {ep['path']}   handler: {ep['function']}

Generate these sub-sections using Markdown:
**Request**
- Path Parameters: (list `{{param}}` placeholders from the path, or "None")
- Query Parameters: (list common query params implied by handler name, or "None")
- Request Body: (for POST/PUT: describe expected JSON fields, or "None")

**Response**
- `200 OK`: describe the success response shape
- `404 Not Found`: when this occurs
- `422 Unprocessable Entity`: validation error (standard FastAPI behaviour)

**Example (curl):**
```bash
# concise curl example
```"""
            
            details = llm.generate(
                _with_code_context(detail_prompt, (project_info or {}).get("code_context", "")),
                max_tokens=2000, temperature=0.15
            )
            doc += details + "\n\n---\n\n"
    
    return doc


def generate_architecture_doc(llm: QwenLLM, project_info: Dict) -> str:
    """Generate system architecture document."""

    evidence_summary = _build_evidence_summary(project_info, project_info.get("endpoints", []), project_info.get("models", []))
    endpoints = project_info.get("endpoints", [])
    models = project_info.get("models", [])
    entry_points = ", ".join(project_info.get("entry_points", [])[:6]) or "not detected"
    folders = ", ".join(project_info.get("folders", [])[:20]) or "not detected"
    frameworks = ", ".join(project_info.get("frameworks", [])) or "none"
    languages = ", ".join(project_info.get("languages", [])) or "not detected"
    ep_list = "\n".join(f"  {e['method']} {e['path']} -> {e['function']}()" for e in endpoints[:20]) or "  None"
    model_list = "\n".join(f"  {m['name']}: " + ", ".join(c['name'] for c in m['columns'][:6]) for m in models[:10]) or "  None"

    template_block = _template_instruction("ARCHITECTURE")

    prompt = f"""You are a senior software architect producing an Architecture Document.
Base EVERY claim on the evidence below. If something is not evidenced, write
\"Not detected from repository evidence\" — do not invent it.

=== EVIDENCE ===
{evidence_summary}

Folder structure: {folders}
Entry points: {entry_points}
Frameworks: {frameworks}
Languages: {languages}

Detected API routes:
{ep_list}

Detected DB models:
{model_list}
================

{template_block}

Formatting: backticks for symbol names, tables for structured data, numbered lists for
sequences. Target 300–500 lines. Every claim MUST reference a real symbol or file."""
    arch_doc = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=6000, temperature=0.1)

    if _is_llm_error(arch_doc):
        arch_doc = (
            "# System Overview\n\n"
            f"## High-level description\nArchitecture derived from repository evidence.\n\n"
            "## Architecture Pattern\n"
            "- Pattern: **Modular application architecture**\n\n"
            "## Technology Stack\n"
            f"- Languages: {languages}\n"
            f"- Frameworks: {frameworks}\n"
        )
    
    return (
        f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n"
        f"<!-- Model: {llm.model} -->\n\n"
    ) + arch_doc


def generate_installation_guide(llm: QwenLLM, project_info: Dict) -> str:
    """Generate detailed installation guide."""
    
    languages = ", ".join(project_info['languages']) or "Not detected"
    frameworks = ", ".join(project_info['frameworks']) if project_info['frameworks'] else "None"
    deps = project_info.get('dependencies', {})
    py_deps = deps.get('python', [])
    js_deps = deps.get('javascript', [])
    config_files = ", ".join(project_info.get('config_files', [])) or "None"
    entry_points = project_info.get('entry_points', [])
    entry_cmd = f"python {entry_points[0]}" if entry_points else "python <entry>.py"
    env_vars = project_info.get('env_vars', [])
    env_vars_str = ", ".join(env_vars[:30]) if env_vars else "None detected"

    template_block = _template_instruction("INSTALLATION")

    prompt = f"""You are a technical writer producing a developer Installation Guide.
Write ONLY what is directly evidenced by the project facts below. Use exact file names.

Project facts:
- Languages: {languages}
- Frameworks: {frameworks}
- Python dependencies (from requirements.txt): {', '.join(py_deps[:25]) or 'see requirements.txt'}
- JavaScript dependencies: {', '.join(js_deps[:15]) or 'none'}
- Config/dependency files present: {config_files}
- Entry points detected: {', '.join(entry_points) or 'not detected'}
- Inferred startup command: `{entry_cmd}`
- Environment variables found in code: {env_vars_str}

{template_block}

Formatting rules: use fenced code blocks for all commands, backticks for file names,
numbered steps everywhere. Make it beginner-friendly.
Only document steps that are supported by the detected facts above."""
    guide = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=4000, temperature=0.2)
    
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + guide


def generate_database_docs(llm: QwenLLM, models: List[Dict], project_info: Dict = None) -> Optional[str]:
    """Generate database schema documentation."""
    
    if not models:
        stub = (
            "# Database Schema Documentation\n\n"
            f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            "No SQLAlchemy models were detected in this repository.\n\n"
            "If the project uses a database, ensure models inherit from a "
            "`Base` class and re-run `fox generate-docs`.\n"
        )
        return stub
    
    doc = "# Database Schema Documentation\n\n"
    doc += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    doc += f"**Total Models:** {len(models)}\n\n"
    
    # ER overview — one LLM call with all models
    model_summary_json = json.dumps(
        [{"name": m['name'], "columns": [c['name'] for c in m['columns'][:12]]} for m in models],
        indent=2
    )
    db_template_block = _template_instruction("DATABASE")
    er_prompt = f"""You are a database architect. Describe the entity-relationship structure for these models.
Only infer relationships from column names that clearly indicate foreign keys
(e.g. `user_id`, `repo_id`, `parent_commit_id`). Do NOT invent relationships.

Models:
{model_summary_json}

{db_template_block}

Write a concise ER overview (3-6 sentences) followed by a Markdown table:
| Entity | Relates To | Relationship Type | Via Column |
Only include rows where a FK column is explicitly listed above."""
    doc += "## Entity Relationship Overview\n\n"
    er_desc = llm.generate(
        _with_code_context(er_prompt, (project_info or {}).get("code_context", "")),
        max_tokens=2500, temperature=0.15
    )
    doc += er_desc + "\n\n---\n\n"
    
    doc += "## Models\n\n"
    
    for model in models:
        doc += f"### {model['name']}\n\n"
        
        if model['docstring']:
            doc += f"{model['docstring']}\n\n"
        
        doc += f"**Source:** `{model['file']}`\n\n"
        
        if model['columns']:
            # One LLM call for all columns in this model
            col_defs = "\n".join(
                f"  {c['name']} = Column({c['definition']})"
                for c in model['columns'][:20]
            )
            col_prompt = f"""For the `{model['name']}` database model, describe each column in one concise sentence.
Respond ONLY as a JSON object mapping column name to description string.
Base descriptions strictly on the column name and SQLAlchemy Column definition.

Columns:
{col_defs}

JSON format: {{"column_name": "one-sentence description", ...}}"""

            col_resp = llm.generate(
                col_prompt, max_tokens=2000, temperature=0.15
            )
            # Parse JSON response
            col_descriptions: Dict[str, str] = {}
            try:
                raw = col_resp.strip()
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                col_descriptions = json.loads(raw)
            except Exception:
                pass  # fall back to empty descriptions

            doc += "**Columns:**\n\n"
            doc += "| Column | Type | Description |\n"
            doc += "|--------|------|-------------|\n"
            for col in model['columns']:
                col_type = col['definition'][:60].rstrip(",")
                col_desc = col_descriptions.get(col['name'], "").replace("|", "-")
                doc += f"| `{col['name']}` | `{col_type}` | {col_desc} |\n"
            doc += "\n"
        
        doc += "---\n\n"
    
    return doc


def generate_srs_requirements(llm: QwenLLM, project_info: Dict, endpoints: List, models: List) -> Optional[str]:
    """Generate Software Requirements Specification (SRS) document."""
    
    if project_info['file_count'] < 2:
        return None
    
    ep_list = "\n".join(f"  {e['method']} {e['path']} -> {e['function']}()" for e in endpoints[:25]) or "  None detected"
    model_list = "\n".join(f"  {m['name']}: " + ", ".join(c['name'] for c in m['columns'][:8]) for m in models[:12]) or "  None detected"
    languages = ", ".join(project_info['languages']) or "Not detected"
    frameworks = ", ".join(project_info['frameworks']) if project_info['frameworks'] else "None"
    signals = ", ".join((project_info.get('import_signals') or [])[:20]) or "None"

    template_block = _template_instruction("REQUIREMENTS")

    prompt = f"""You are a requirements analyst writing a formal Software Requirements Specification (SRS).
Anchor EVERY requirement to real, detected code artefacts below. Do NOT invent features.

=== DETECTED ARTEFACTS ===
Project: {project_info['name']}
Languages: {languages}
Frameworks: {frameworks}
Import signals: {signals}
Files: {project_info['file_count']}  Lines: {project_info['line_count']}

API Endpoints ({len(endpoints)}):
{ep_list}

Database Models ({len(models)}):
{model_list}
==========================

{template_block}

Formatting: use Markdown tables for structured data, numbered IDs for each requirement.
Write at least one FR per detected API endpoint and one per DB model."""
    srs = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=6000, temperature=0.2)
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + srs


def generate_use_cases(llm: QwenLLM, project_info: Dict, endpoints: List, models: List) -> Optional[str]:
    """Generate Use Cases and User Stories document."""
    
    if not endpoints and not models and project_info['file_count'] < 2:
        return None
    
    ep_list = "\n".join(f"  {e['method']} {e['path']} -> {e['function']}()" for e in endpoints[:20]) or "  None detected"
    model_names = ", ".join(m['name'] for m in models[:10]) or "None detected"
    languages = ", ".join(project_info['languages']) or "Not detected"
    frameworks = ", ".join(project_info['frameworks']) if project_info['frameworks'] else "None"

    template_block = _template_instruction("USE_CASES")

    prompt = f"""You are a business analyst writing Use Cases and User Stories.
Every use case flow step MUST reference a real API endpoint or class name from the evidence below.

=== EVIDENCE ===
Project: {project_info['name']}
Languages: {languages}  Frameworks: {frameworks}

API Endpoints ({len(endpoints)}):
{ep_list}

DB Models: {model_names}
================

{template_block}

For each Use Case, cite the exact endpoint method+path that implements the flow step.
Generate 5–8 detailed use cases and 10–15 user stories with acceptance criteria.
Do NOT invent actors or workflows not supported by the evidence."""
    use_cases = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=5000, temperature=0.2)
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + use_cases


def generate_release_notes(llm: QwenLLM, project_info: Dict) -> Optional[str]:
    """Generate Release Notes / Changelog document."""
    
    if project_info['file_count'] < 2:
        return None
    
    endpoints = project_info.get('endpoints', [])
    models = project_info.get('models', [])
    languages = ", ".join(project_info['languages']) or "Not detected"
    frameworks = ", ".join(project_info['frameworks']) if project_info['frameworks'] else "None"
    ep_list = ", ".join(f"{e['method']} {e['path']}" for e in endpoints[:12]) or "None"
    model_names = ", ".join(m['name'] for m in models[:8]) or "None"
    signals = ", ".join((project_info.get('import_signals') or [])[:15]) or "None"

    template_block = _template_instruction("RELEASE_NOTES")

    prompt = f"""You are a product manager writing Release Notes.
Derive EVERY feature bullet from the real code artefacts below. Do NOT invent features.

=== EVIDENCE ===
Project: {project_info['name']}
Languages: {languages}  Frameworks: {frameworks}
Files: {project_info['file_count']}  Lines: {project_info['line_count']}
Import signals: {signals}
Endpoints: {ep_list}
DB models: {model_names}
================

{template_block}

Each feature bullet MUST cite the implementing endpoint, model, or function name in backticks.
Only describe limitations and known issues that are plausibly real given the codebase.
Release date: {datetime.now().strftime('%Y-%m-%d')}"""
    release_notes = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=4000, temperature=0.2)
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + release_notes


def generate_user_manual(llm: QwenLLM, project_info: Dict, endpoints: List) -> Optional[str]:
    """Generate User Manual and Admin Guide."""
    
    if not endpoints and project_info['file_count'] < 2:
        return None
    
    ep_list = "\n".join(f"  {e['method']} {e['path']} -> {e['function']}()" for e in endpoints[:20]) or "  None"
    languages = ", ".join(project_info['languages']) or "Not detected"
    frameworks = ", ".join(project_info['frameworks']) if project_info['frameworks'] else "None"

    template_block = _template_instruction("USER_MANUAL")

    prompt = f"""You are a technical writer producing a User Manual.
Every task/workflow described MUST map to a real API endpoint or feature evidenced below.

=== EVIDENCE ===
Project: {project_info['name']}
Languages: {languages}  Frameworks: {frameworks}

API Endpoints ({len(endpoints)}):
{ep_list}
================

{template_block}

Formatting: use numbered steps for procedures, tables for reference data, code blocks for
commands and curl examples. Every task must cite the specific API route it exercises."""
    manual = llm.generate(_with_code_context(prompt, project_info.get("code_context", "")), max_tokens=4000, temperature=0.3)
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + manual


def generate_api_usage_guide(llm: QwenLLM, endpoints: List[Dict], project_info: Dict) -> Optional[str]:
    """Generate API Usage Guide with practical examples."""
    
    if not endpoints:
        stub = (
            "# API Usage Guide\n\n"
            f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            "No API endpoints were detected. Please see the User Manual for CLI/desktop usage.\n"
        )
        return stub
    
    doc = "# API Usage Guide\n\n"
    doc += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    doc += f"**Project:** {project_info['name']}\n\n"
    
    all_routes = "\n".join(
        f"  {e['method']:6} {e['path']:50} -> {e['function']}()"
        for e in endpoints[:30]
    )
    
    # Authentication & setup — grounded in code context
    api_usage_template_block = _template_instruction("API_USAGE")
    auth_prompt = f"""Based on the code evidence provided, write an 'Authentication & Setup' section for developers
who want to call this API.

All detected routes:
{all_routes}

Include:
- Base URL (infer from route patterns like /api/...)
- Auth mechanism (look for Bearer token, JWT, or API key patterns in code context)
- Required headers (Content-Type, Authorization)
- A minimal curl example to authenticate

Only describe authentication that is directly evidenced. Start with '## Authentication & Setup'.

{api_usage_template_block}"""
    auth_section = llm.generate(
        _with_code_context(auth_prompt, project_info.get("code_context", "")),
        max_tokens=700, temperature=0.2
    )
    doc += auth_section + "\n\n---\n\n"
    
    doc += "## Endpoint Examples\n\n"
    
    # Generate one rich example per endpoint (up to 6)
    for idx, ep in enumerate(endpoints[:6], 1):
        doc += f"### {idx}. `{ep['method']} {ep['path']}`\n\n"
        doc += f"**Handler:** `{ep['function']}()`  \n"
        _ep_internal = (
            '_internal' in ep['path']
            or ep['path'].endswith('/internal')
            or '/admin/' in ep['path']
            or ep['function'].startswith('_')
        )
        if _ep_internal:
            doc += "> 🔒 **Internal Endpoint** — not intended for external API consumers.\n\n"
        elif '/admin' in ep['path'] or 'admin' in ep['function'].lower():
            doc += "> 🛡️ **Admin Endpoint** — requires administrative privileges.\n\n"
        if ep.get('docstring'):
            doc += f"{ep['docstring']}\n\n"
        
        example_prompt = f"""Generate a complete, copy-pasteable usage example for:

{ep['method']} {ep['path']}   handler: {ep['function']}

Sections to include:

**Request:**
```bash
# curl example with real placeholder values
```

**Request body (if POST/PUT):**
```json
// JSON with plausible field names derived from route/handler name
```

**Success response (`200`):**
```json
// realistic response JSON
```

**Error responses:** list HTTP codes that apply.

**Python example:**
```python
import requests
# minimal working example
```

Use realistic placeholder values. Do NOT invent fields not implied by the endpoint path/name."""
        
        example = llm.generate(
            _with_code_context(example_prompt, project_info.get("code_context", "")),
            max_tokens=2000, temperature=0.15
        )
        doc += example + "\n\n---\n\n"
    
    # Common patterns — grounded in detected routes
    has_list_routes = any('list' in e['function'].lower() or e['method'] == 'GET' for e in endpoints)
    patterns_prompt = f"""Write a 'Common Patterns' section for the {project_info.get('name', 'project')} API.
Base every pattern on the detected routes above.

Include these sub-sections (skip any not evidenced):
1. Listing resources (pagination) — only if GET list endpoints detected
2. Error handling — show how to handle 4xx/5xx responses
3. Authentication flow — if auth endpoints detected
4. Creating and updating resources — if POST/PUT endpoints detected

Use curl and Python `requests` examples."""
    
    patterns = llm.generate(
        _with_code_context(patterns_prompt, project_info.get("code_context", "")),
        max_tokens=2000, temperature=0.15
    )
    doc += "## Common Patterns\n\n" + patterns + "\n\n"
    
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + doc


def generate_database_er_detailed(llm: QwenLLM, models: List[Dict], project_info: Dict = None) -> Optional[str]:
    """Generate detailed Database ER documentation with relationships."""
    
    if not models:
        stub = (
            "# Database Entity-Relationship Documentation\n\n"
            f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            "No database models were detected.\n"
        )
        return stub
    
    doc = "# Database Entity-Relationship Documentation\n\n"
    doc += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    doc += f"**Total Entities:** {len(models)}\n\n"
    
    db_er_template_block = _template_instruction("DATABASE_ER")

    # Generate comprehensive ER overview
    er_prompt = f"""Analyze these database entities and generate a comprehensive ER documentation:

{json.dumps([{{"name": m['name'], "columns": [c['name'] for c in m['columns'][:15]]}} for m in models], indent=2)}

{db_er_template_block}

Make it technically accurate and detailed. Identify all relationships (1:1, 1:N, N:M),
explain cardinality, and create a relationship matrix table."""
    er_overview = llm.generate(_with_code_context(er_prompt, (project_info or {}).get("code_context", "")), max_tokens=4000, temperature=0.15)
    doc += er_overview + "\n\n---\n\n"
    
    # Detailed entity analysis
    doc += "## Detailed Entity Analysis\n\n"
    
    for model in models:
        doc += f"### Entity: {model['name']}\n\n"
        
        entity_prompt = f"""Analyze this database entity in detail:

Entity: {model['name']}
Columns: {json.dumps([{{"name": c['name'], "type": c['definition']}} for c in model['columns'][:15]], indent=2)}

Generate:
1. **Purpose:** What this entity represents
2. **Primary Key:** Identify and explain
3. **Foreign Keys:** List and explain relationships
4. **Indexes:** Recommended indexes for performance
5. **Constraints:** Business rules and constraints
6. **Sample Queries:** 3 common SQL queries for this entity

Be specific and technical."""
        
        entity_analysis = llm.generate(_with_code_context(entity_prompt, (project_info or {}).get("code_context", "")), max_tokens=2000, temperature=0.15)
        doc += entity_analysis + "\n\n"
        
        if model['columns']:
            doc += "**Column Reference:**\n\n"
            doc += "| Column | Type | Nullable | Description |\n"
            doc += "|--------|------|----------|-------------|\n"
            
            for col in model['columns'][:20]:
                col_name = col['name']
                col_type = col['definition'][:50]
                col_desc_prompt = f"Describe database column in one sentence: {col_name} ({col['definition']})"
                col_desc = llm.generate(_with_code_context(col_desc_prompt, (project_info or {}).get("code_context", "")), max_tokens=80, temperature=0.3)
                doc += f"| {col_name} | `{col_type}` | ? | {col_desc} |\n"
            
            doc += "\n"
        
        doc += "---\n\n"
    
    # Add SQL examples section
    sql_prompt = f"""Generate a 'Common SQL Patterns' section with practical examples for these entities:

{', '.join([m['name'] for m in models[:5]])}

Include:
1. JOIN examples (INNER, LEFT, etc.)
2. Aggregation queries
3. Complex filtering
4. Transaction examples
5. Performance optimization tips

Provide complete, runnable SQL."""
    
    sql_examples = llm.generate(_with_code_context(sql_prompt, (project_info or {}).get("code_context", "")), max_tokens=2000, temperature=0.2)
    doc += "## Common SQL Patterns\n\n" + sql_examples + "\n\n"
    
    return f"<!-- Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n" + doc


def _normalize_for_similarity(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    lowered = re.sub(r"```[\s\S]*?```", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _is_code_heavy(text: str) -> bool:
    if not text:
        return True
    fenced_blocks = text.count("```") // 2
    lines = text.splitlines()
    if not lines:
        return True
    code_like = 0
    for line in lines:
        if re.match(r"^\s*(import\s+|from\s+\S+\s+import\s+|def\s+\w+\(|class\s+\w+|function\s+\w+|const\s+\w+|let\s+\w+|var\s+\w+|public\s+\w+|private\s+\w+|#include\s+|SELECT\s+|INSERT\s+|UPDATE\s+|DELETE\s+)", line):
            code_like += 1
    ratio = code_like / max(1, len(lines))
    return fenced_blocks >= 2 or ratio > 0.18


def _max_similarity_to_prior(content: str, prior_contents: List[str]) -> float:
    if not content or not prior_contents:
        return 0.0
    current = _normalize_for_similarity(content)[:6000]
    best = 0.0
    for prev in prior_contents:
        prev_norm = _normalize_for_similarity(prev)[:6000]
        if not prev_norm:
            continue
        score = SequenceMatcher(None, current, prev_norm).ratio()
        if score > best:
            best = score
    return best


def _needs_quality_retry(doc_name: str, content: str, prior_contents: List[str]) -> Optional[str]:
    if not content:
        return "empty output"
    if "[LLM Error:" in content:
        return "llm error output"
    if _is_code_heavy(content):
        return "output contains too much raw code"
    sim = _max_similarity_to_prior(content, prior_contents)
    if sim >= 0.72:
        return f"content is too similar to another generated document (similarity={sim:.2f})"
    return None


def _rewrite_document_with_constraints(
    llm: QwenLLM,
    doc_name: str,
    draft: str,
    project_info: Dict[str, Any],
    reason: str,
) -> str:
    template_key = _template_key_for_doc_name(doc_name)
    template_block = _template_instruction(template_key) if template_key else ""
    rewrite_prompt = f"""You are rewriting one documentation artifact to improve quality.

Document: {doc_name}
Project: {project_info.get('name', 'Unknown project')}
Rewrite reason: {reason}

STRICT RULES:
1) Do NOT include raw source code blocks (no fenced code longer than 3 lines).
2) Keep this document focused ONLY on its own purpose; avoid reusing generic paragraphs from other docs.
3) Use concrete names from the project evidence (modules, endpoints, models, services) and avoid unrelated libraries.
4) Use concise, technical prose and tables where useful.
5) Preserve the required document template structure.

{template_block}

Current draft to improve:
---
{draft[:20000]}
---

Return the improved {doc_name} content only."""

    rewritten = llm.generate(
        _with_code_context(rewrite_prompt, project_info.get("code_context", "")),
        max_tokens=5000,
        temperature=0.15,
    )
    return rewritten if rewritten else draft


def generate_project_docs(project_path: str, output_dir: str = None, llm_model: str = "qwen2.5-coder:7b", project_name: str = None, selected_docs: Optional[List[str]] = None, progress_callback=None) -> Dict[str, Any]:
    """
    Generate comprehensive project documentation.
    Only generates .docx files (except README.md which stays as .md).
    Skips documents when insufficient project signals detected.
    
    Returns:
        Dict with generated files, skipped docs with reasons, and statistics.
    """
    project_path = Path(project_path)
    
    if output_dir is None:
        output_dir = project_path / "docs" / "project"
    else:
        output_dir = Path(output_dir)
    
    output_dir.mkdir(parents=True, exist_ok=True)

    max_runtime_seconds = int(os.getenv("FOXNEST_PROJECT_DOCS_MAX_SECONDS", "2700"))
    started_at = time.time()

    def _time_budget_exceeded() -> bool:
        return (time.time() - started_at) > max_runtime_seconds
    
    selected_doc_names, invalid_selected_docs = parse_selected_project_docs(selected_docs)
    if invalid_selected_docs:
        return {
            "generated_files": [],
            "skipped_docs": [],
            "project_info": {},
            "stats": {},
            "error": f"Invalid doc names: {', '.join(invalid_selected_docs)}",
        }

    # Initialize LLM
    llm = QwenLLM(model=llm_model)
    llm.validate_runtime()
    global _CODE_CONTEXT_CHAR_LIMIT
    _CODE_CONTEXT_CHAR_LIMIT = _context_limit_for_model(llm.model)

    def _cb(name: str, status: str):
        if progress_callback:
            try:
                progress_callback(name, status)
            except Exception:
                pass

    _cb("Setup", "generating")
    # Analyze project
    print("📊 Analyzing project structure...")
    project_info = analyze_project_structure(project_path)
    
    if project_name:
        project_info["name"] = project_name
    
    print("🔍 Extracting API endpoints...")
    endpoints = extract_fastapi_endpoints(project_path)
    
    print("🗄️  Extracting database models...")
    models = extract_database_models(project_path)

    project_info["endpoints"] = endpoints
    project_info["models"] = models

    print("📚 Building code-aware context for LLM...")
    project_info["code_context"] = collect_code_context(project_path, project_info, endpoints, models)

    # Scan existing docs so LLM matches team style/terminology
    print("📄 Reading existing documentation for style reference...")
    existing_style = _scan_existing_docs(project_path)
    if existing_style:
        project_info["code_context"] = (
            existing_style + "\n\n---\n\n" + project_info["code_context"]
        )
    _cb("Setup", "done")
    
    results = {
        "generated_files": [],
        "skipped_docs": [],
        "project_info": project_info,
        "stats": {
            "endpoints": len(endpoints),
            "models": len(models),
            "llm_model": llm.model,
            "llm_calls": 0,
            "template_digest": _template_digest(),
            "max_runtime_seconds": max_runtime_seconds,
            "selected_docs": sorted(selected_doc_names) if selected_doc_names else ["all"],
        }
    }
    
    # Document generation map: (name, generator_func, args, filename_stem)
    doc_configs = [
        ("README.md", generate_readme, (llm, project_info, endpoints, models), "README", True),  # Keep as .md
        ("API Documentation", generate_api_docs, (llm, endpoints, project_info), "API", False),
        ("Architecture", generate_architecture_doc, (llm, project_info), "ARCHITECTURE", False),
        ("Installation Guide", generate_installation_guide, (llm, project_info), "INSTALLATION", False),
        ("Database Schema", generate_database_docs, (llm, models, project_info), "DATABASE", False),
        ("Requirements (SRS)", generate_srs_requirements, (llm, project_info, endpoints, models), "REQUIREMENTS", False),
        ("Use Cases", generate_use_cases, (llm, project_info, endpoints, models), "USE_CASES", False),
        ("Release Notes", generate_release_notes, (llm, project_info), "RELEASE_NOTES", False),
        ("User Manual", generate_user_manual, (llm, project_info, endpoints), "USER_MANUAL", False),
        ("API Usage Guide", generate_api_usage_guide, (llm, endpoints, project_info), "API_USAGE", False),
        ("Database ER", generate_database_er_detailed, (llm, models, project_info), "DATABASE_ER", False),
    ]
    
    prior_doc_contents: List[str] = []

    # Generate documents
    for doc_name, generator, args, filename, keep_md in doc_configs:
        if selected_doc_names is not None and doc_name not in selected_doc_names:
            reason = "Not selected by user"
            results["skipped_docs"].append({"name": doc_name, "reason": reason})
            _cb(doc_name, "skipped")
            continue

        if _time_budget_exceeded():
            reason = f"Time budget exceeded ({max_runtime_seconds}s). Generation stopped with partial output."
            results["skipped_docs"].append({"name": doc_name, "reason": reason})
            print(f"   ⏭️  Skipped - {reason}")
            _cb(doc_name, "skipped")
            continue

        print(f"📝 Generating {doc_name}...")
        _cb(doc_name, "generating")

        template_key = _template_key_for_doc_name(doc_name)
        if template_key and not _load_template(template_key):
            reason = _missing_template_reason(template_key)
            results["skipped_docs"].append({"name": doc_name, "reason": reason})
            print(f"   ⏭️  Skipped - {reason}")
            _cb(doc_name, "skipped")
            continue
        
        try:
            content = generator(*args)
            
            if content is None:
                # Skip this document
                reason = _get_skip_reason(doc_name, endpoints, models, project_info)
                results["skipped_docs"].append({"name": doc_name, "reason": reason})
                print(f"   ⏭️  Skipped - {reason}")
                _cb(doc_name, "skipped")
                continue

            if "[LLM Error:" in content:
                reason = f"LLM returned an error for {doc_name}: {content[:180]}"
                results["skipped_docs"].append({"name": doc_name, "reason": reason})
                print(f"   ⏭️  Skipped - {reason}")
                _cb(doc_name, "skipped")
                continue

            retry_reason = _needs_quality_retry(doc_name, content, prior_doc_contents)
            if retry_reason:
                print(f"   ⚠ Quality retry for {doc_name}: {retry_reason}")
                improved = _rewrite_document_with_constraints(llm, doc_name, content, project_info, retry_reason)
                second_reason = _needs_quality_retry(doc_name, improved, prior_doc_contents)
                if not second_reason:
                    content = improved

            prior_doc_contents.append(content)
            
            if keep_md:
                # Keep as Markdown (README only)
                file_path = output_dir / f"{filename}.md"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                results["generated_files"].append(str(file_path))
                print(f"   ✅ Generated {filename}.md")
                _cb(doc_name, "done")
            else:
                # Generate DOCX directly
                docx_path = output_dir / f"{filename}.docx"
                if docx_utils.create_docx_directly(content, docx_path, title=doc_name):
                    results["generated_files"].append(str(docx_path))
                    print(f"   ✅ Generated {filename}.docx")
                    _cb(doc_name, "done")
                else:
                    print(f"   ❌ Failed to create {filename}.docx")
                    _cb(doc_name, "error")
        
        except Exception as e:
            if isinstance(e, LLMFatalError):
                raise
            print(f"   ❌ Error generating {doc_name}: {str(e)}")
            results["skipped_docs"].append({"name": doc_name, "reason": f"Error: {str(e)}"})
            _cb(doc_name, "error")

    
    # Generate index.md (always as markdown for navigation)
    # Follows TEMPLATE_INDEX.md structure for consistency
    languages_str = ", ".join(project_info.get("languages", [])) or "—"
    frameworks_str = ", ".join(project_info.get("frameworks", [])) or "—"
    _ep_count   = len(project_info.get("endpoints", []))
    _model_count = len(project_info.get("models", []))
    _file_count  = project_info.get("file_count", 0)
    _line_count  = project_info.get("line_count", 0)

    index = f"# Project Documentation: {project_info['name']}\n\n"
    index += f"*Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    index += f"---\n\n"

    # Core doc table
    SECTION_MAP = {
        "README":       ("Core Documents",     "Project overview, quick start, and structure"),
        "ARCHITECTURE": ("Core Documents",     "System design, components, data flow"),
        "INSTALLATION": ("Core Documents",     "Step-by-step setup instructions"),
        "API":          ("API Reference",      "All endpoints with requests/responses"),
        "API_USAGE":    ("API Reference",      "Integration guide, code examples, best practices"),
        "DATABASE":     ("Data & Schema",      "Table definitions, column types"),
        "DATABASE_ER":  ("Data & Schema",      "Entity relationships and cardinality"),
        "REQUIREMENTS": ("Requirements",       "Functional and non-functional requirements"),
        "USE_CASES":    ("Requirements",       "Actor-driven use case scenarios"),
        "USER_MANUAL":  ("User & Operations",  "End-user guide with FAQ"),
        "RELEASE_NOTES":("User & Operations",  "Version history and upgrade guides"),
    }
    STEM_TO_KEY = {
        "README": "README", "API": "API", "ARCHITECTURE": "ARCHITECTURE",
        "INSTALLATION": "INSTALLATION", "DATABASE": "DATABASE",
        "REQUIREMENTS": "REQUIREMENTS", "USE_CASES": "USE_CASES",
        "RELEASE_NOTES": "RELEASE_NOTES", "USER_MANUAL": "USER_MANUAL",
        "API_USAGE": "API_USAGE", "DATABASE_ER": "DATABASE_ER",
    }

    # Group generated files by section
    section_files: Dict[str, list] = {}
    for file_path in results["generated_files"]:
        p = Path(file_path)
        stem = p.stem.upper()
        key = STEM_TO_KEY.get(stem, "")
        section_label = SECTION_MAP.get(key, ("Other", ""))[0] if key else "Other"
        desc = SECTION_MAP.get(key, ("", "Document"))[1] if key else ""
        section_files.setdefault(section_label, []).append((p.name, desc))

    section_order = ["Core Documents", "API Reference", "Data & Schema", "Requirements", "User & Operations", "Other"]
    for section_label in section_order:
        entries = section_files.get(section_label)
        if not entries:
            continue
        index += f"## {section_label}\n\n"
        index += "| Document | File | Description |\n"
        index += "|----------|------|-------------|\n"
        for fname, fdesc in entries:
            display_name = Path(fname).stem.replace("_", " ").title()
            index += f"| **{display_name}** | [{fname}]({fname}) | {fdesc} |\n"
        index += "\n"

    if results["skipped_docs"]:
        index += "## Skipped Documents\n\n"
        index += "| Document | Reason |\n"
        index += "|----------|--------|\n"
        for skipped in results["skipped_docs"]:
            index += f"| {skipped['name']} | {skipped['reason']} |\n"
        index += "\n"

    index += "---\n\n"
    index += "## Project Summary\n\n"
    index += f"| Property | Value |\n"
    index += f"|----------|-------|\n"
    index += f"| Project Name | {project_info['name']} |\n"
    index += f"| Primary Language | {languages_str} |\n"
    index += f"| Framework | {frameworks_str} |\n"
    index += f"| API Endpoints | {_ep_count} detected |\n"
    index += f"| Database Models | {_model_count} detected |\n"
    index += f"| Source Files | {_file_count} files, {_line_count:,} lines |\n"
    index += f"| Generated On | {datetime.now().strftime('%Y-%m-%d %H:%M')} |\n"
    index += f"| LLM Model | {llm.model} |\n\n"
    index += "---\n\n"
    index += "*Auto-generated by FoxNest documentation engine. Do not edit manually — regenerate with `fox generate-docs`.*\n"


    index_path = output_dir / "index.md"
    if selected_doc_names is not None and "index.md" not in selected_doc_names:
        reason = "Not selected by user"
        results["skipped_docs"].append({"name": "index.md", "reason": reason})
        print(f"   ⏭️  Skipped index.md - {reason}")
        results["stats"]["llm_calls"] = llm.call_count
        return results

    if not _load_template("INDEX"):
        reason = _missing_template_reason("INDEX")
        results["skipped_docs"].append({"name": "index.md", "reason": reason})
        print(f"   ⏭️  Skipped index.md - {reason}")
        results["stats"]["llm_calls"] = llm.call_count
        return results

    index += f"\n<!-- template-digest: {results['stats']['template_digest']} -->\n"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index)
    results["generated_files"].append(str(index_path))
    
    print(f"\n✅ Generated: {len(results['generated_files'])} files")
    print(f"⏭️  Skipped: {len(results['skipped_docs'])} documents")

    results["stats"]["llm_calls"] = llm.call_count
    
    return results


def _get_skip_reason(doc_name: str, endpoints: List, models: List, project_info: Dict) -> str:
    """Get human-readable reason for skipping a document."""
    if "API" in doc_name:
        return f"No API endpoints detected (found {len(endpoints)})"
    elif "Database" in doc_name or "ER" in doc_name:
        return f"No database models detected (found {len(models)})"
    elif "Requirements" in doc_name or "SRS" in doc_name:
        return f"Project too small for SRS (found {project_info['file_count']} files, need ≥2)"
    elif "Use Cases" in doc_name:
        return f"Project too small for Use Cases (found {project_info['file_count']} files, need ≥2)"
    elif "Release Notes" in doc_name:
        return f"Project too small for Release Notes (found {project_info['file_count']} files, need ≥2)"
    elif "User Manual" in doc_name:
        return f"Project too small for User Manual (found {project_info['file_count']} files, need ≥2)"
    else:
        return "Insufficient project data"
