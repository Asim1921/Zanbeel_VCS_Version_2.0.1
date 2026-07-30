#!/usr/bin/env python3
"""
FoxNest Auto-Docs Generator
Detects programming languages in a project and generates documentation automatically.
Supports: Python, JavaScript/TypeScript, Java, C#, Go, Rust, C/C++, PHP, Ruby, Swift, Kotlin, SQL, Dart, Shell
"""

import os
import ast
import re
import json
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    from llm_helper import get_llm_helper
except ImportError:
    def get_llm_helper():
        return None


# ─── Language Detection ──────────────────────────────────────────────────────

LANGUAGE_EXTENSIONS = {
    "python":      {".py"},
    "javascript":  {".js", ".jsx", ".mjs", ".cjs"},
    "typescript":  {".ts", ".tsx"},
    "java":        {".java"},
    "csharp":      {".cs"},
    "go":          {".go"},
    "rust":        {".rs"},
    "c":           {".c", ".h"},
    "cpp":         {".cpp", ".cxx", ".cc", ".hpp", ".hxx"},
    "php":         {".php"},
    "ruby":        {".rb"},
    "swift":       {".swift"},
    "kotlin":      {".kt", ".kts"},
    "sql":         {".sql"},
    "dart":        {".dart"},
    "shell":       {".sh", ".bash", ".zsh"},
    "r":           {".r", ".R"},
    "lua":         {".lua"},
    "scala":       {".scala"},
    "elixir":      {".ex", ".exs"},
    "xml":         {".xml"},
}

IGNORE_DIRS = {
    ".fox", ".git", ".svn", ".hg",
    "venv", "env", ".venv", ".env",
    "node_modules", "__pycache__", ".pytest_cache",
    ".tox", ".coverage", "dist", "build",
    ".DS_Store", "Thumbs.db", ".idea", ".vscode",
    "vendor", "target", "bin", "obj",
    "docs", "_docs",
}


def detect_languages(project_path):
    """Scan project directory and detect programming languages used."""
    lang_files = defaultdict(list)
    ext_to_lang = {}
    for lang, exts in LANGUAGE_EXTENSIONS.items():
        for ext in exts:
            ext_to_lang[ext] = lang

    project = Path(project_path)
    for file_path in project.rglob("*"):
        if file_path.is_file():
            parts = file_path.relative_to(project).parts
            if any(p in IGNORE_DIRS or p.startswith(".") for p in parts):
                continue
            ext = file_path.suffix.lower()
            if ext in ext_to_lang:
                lang_files[ext_to_lang[ext]].append(str(file_path.relative_to(project)))

    return dict(lang_files)


def detect_frameworks(project_path):
    """Detect frameworks and build tools used in the project."""
    project = Path(project_path)
    frameworks = []

    checks = [
        ("package.json", "node"),
        ("requirements.txt", "python-pip"),
        ("pyproject.toml", "python-pyproject"),
        ("setup.py", "python-setup"),
        ("Pipfile", "python-pipenv"),
        ("pom.xml", "maven"),
        ("build.gradle", "gradle"),
        ("Cargo.toml", "cargo"),
        ("go.mod", "go-mod"),
        ("Gemfile", "ruby-bundler"),
        ("composer.json", "php-composer"),
        ("pubspec.yaml", "dart-pub"),
        ("Makefile", "make"),
        ("Dockerfile", "docker"),
        ("docker-compose.yml", "docker-compose"),
        ("docker-compose.yaml", "docker-compose"),
    ]

    for filename, framework in checks:
        if (project / filename).exists():
            frameworks.append(framework)

    return frameworks


def _format_summary_block(lines, level=2):
    if not lines:
        return ""
    header = "#" * level + " Summary\n\n"
    return header + "\n".join(f"- {line}" for line in lines) + "\n\n"


def _count_lines(text):
    lines = text.splitlines()
    total = len(lines)
    non_empty = sum(1 for line in lines if line.strip())
    return total, non_empty


def _comment_prefixes_for_lang(lang):
    return {
        "python": ("#",),
        "javascript": ("//", "/*", "*", "*/"),
        "typescript": ("//", "/*", "*", "*/"),
        "java": ("//", "/*", "*", "*/"),
        "csharp": ("//", "/*", "*", "*/"),
        "go": ("//", "/*", "*", "*/"),
        "rust": ("//", "/*", "*", "*/"),
        "c": ("//", "/*", "*", "*/"),
        "cpp": ("//", "/*", "*", "*/"),
        "php": ("//", "/*", "*", "*/", "#"),
        "ruby": ("#",),
        "swift": ("//", "/*", "*", "*/"),
        "kotlin": ("//", "/*", "*", "*/"),
        "sql": ("--",),
        "dart": ("//", "/*", "*", "*/"),
        "shell": ("#",),
        "r": ("#",),
        "lua": ("--",),
        "scala": ("//", "/*", "*", "*/"),
        "elixir": ("#",),
    }.get(lang, ("#", "//", "/*"))


def _count_comment_lines(text, prefixes):
    lines = text.splitlines()
    return sum(1 for line in lines if line.strip().startswith(prefixes))


def _collect_python_imports(tree):
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module.split(".")[0])
    return sorted(set(imports))


def _collect_python_globals(tree):
    globals_found = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            else:
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    globals_found.append(target.id)
    return sorted(set(globals_found))


def _unparse_node(node):
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _format_python_signature(node):
    args = []
    defaults = list(node.args.defaults)
    default_offset = len(node.args.args) - len(defaults)

    for index, arg in enumerate(node.args.args):
        default_node = None
        if index >= default_offset:
            default_node = defaults[index - default_offset]
        annotation = _unparse_node(arg.annotation) if arg.annotation else None
        piece = arg.arg
        if annotation:
            piece = f"{piece}: {annotation}"
        if default_node is not None:
            default_text = _unparse_node(default_node)
            if default_text is not None:
                piece = f"{piece}={default_text}"
        args.append(piece)

    if node.args.vararg:
        vararg = node.args.vararg
        annotation = _unparse_node(vararg.annotation) if vararg.annotation else None
        piece = f"*{vararg.arg}"
        if annotation:
            piece = f"{piece}: {annotation}"
        args.append(piece)

    if node.args.kwonlyargs:
        if not node.args.vararg:
            args.append("*")
        for arg, default_node in zip(node.args.kwonlyargs, node.args.kw_defaults):
            annotation = _unparse_node(arg.annotation) if arg.annotation else None
            piece = arg.arg
            if annotation:
                piece = f"{piece}: {annotation}"
            if default_node is not None:
                default_text = _unparse_node(default_node)
                if default_text is not None:
                    piece = f"{piece}={default_text}"
            args.append(piece)

    if node.args.kwarg:
        kwarg = node.args.kwarg
        annotation = _unparse_node(kwarg.annotation) if kwarg.annotation else None
        piece = f"**{kwarg.arg}"
        if annotation:
            piece = f"{piece}: {annotation}"
        args.append(piece)

    signature = f"{node.name}({', '.join(args)})"
    if node.returns:
        return_type = _unparse_node(node.returns)
        if return_type:
            signature = f"{signature} -> {return_type}"
    return signature


def _snippet_from_node(source, node, max_lines=200):
    """Extract a code snippet for a function/class node.

    Captures the FULL decorator+def/class header and entire body.
    max_lines=200 ensures no function is silently truncated — the LLM
    must see the complete implementation to document it accurately.
    """
    lines = source.splitlines()
    if not lines:
        return ""
    # Start at the first decorator if present, otherwise at the def/class line
    start_line = getattr(node, "lineno", 1)
    if getattr(node, "decorator_list", None):
        start_line = node.decorator_list[0].lineno
    end_line = getattr(node, "end_lineno", None)
    if end_line is None:
        end_line = start_line + max_lines
    start_index = max(start_line - 1, 0)
    # Use the real end_lineno from the AST — no artificial cap unless the
    # function is truly huge (> max_lines); in that case we take the first
    # max_lines lines which still cover the logic far better than 40.
    end_index = min(end_line, start_index + max_lines)
    return "\n".join(lines[start_index:end_index])


def _build_file_context(info: dict, rel_path: str) -> str:
    """Build a compact file-context string that is prepended to every LLM prompt.

    Giving the model this background means it understands the surrounding
    codebase — e.g. what database session object 'db' is, what the imports
    bring in, etc. — leading to far more accurate documentation.
    """
    lines = [f"File: {rel_path}"]
    if info.get("module_docstring"):
        lines.append(f"Module docstring: {info['module_docstring'][:300]}")
    if info.get("imports"):
        lines.append(f"Imports: {', '.join(info['imports'][:15])}")
    if info.get("globals"):
        lines.append(f"Module-level globals: {', '.join(info['globals'][:10])}")
    class_names = [c["name"] for c in info.get("classes", [])]
    if class_names:
        lines.append(f"Classes defined: {', '.join(class_names)}")
    func_names = [f["name"] for f in info.get("functions", [])]
    if func_names:
        lines.append(f"Top-level functions: {', '.join(func_names[:15])}")
    return "\n".join(lines)


def _call_name(call_node):
    if isinstance(call_node.func, ast.Name):
        return call_node.func.id
    if isinstance(call_node.func, ast.Attribute):
        return call_node.func.attr
    return None


def _summarize_python_callable(node):
    statements = len(getattr(node, "body", []))
    assigns = 0
    branches = 0
    loops = 0
    returns = 0
    raises = 0
    calls = []

    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            assigns += 1
        elif isinstance(child, (ast.If, ast.Try, ast.With, ast.Match)):
            branches += 1
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
            loops += 1
        elif isinstance(child, ast.Return):
            returns += 1
        elif isinstance(child, ast.Raise):
            raises += 1
        elif isinstance(child, ast.Call):
            name = _call_name(child)
            if name:
                calls.append(name)

    call_list = []
    seen = set()
    for name in calls:
        if name in seen:
            continue
        seen.add(name)
        call_list.append(name)
        if len(call_list) >= 5:
            break

    lines = [f"Statements: {statements}"]
    if assigns:
        lines.append(f"Assignments: {assigns}")
    if branches:
        lines.append(f"Branches: {branches}")
    if loops:
        lines.append(f"Loops: {loops}")
    if returns:
        lines.append(f"Returns: {returns}")
    if raises:
        lines.append(f"Raises: {raises}")
    if call_list:
        lines.append(f"Calls: {', '.join(call_list)}")

    return lines


def _summarize_python_class(node, methods):
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(base.attr)
    decorators = []
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name):
            decorators.append(deco.id)
        elif isinstance(deco, ast.Attribute):
            decorators.append(deco.attr)

    async_count = sum(1 for m in methods if m.get("is_async"))
    lines = [f"Methods: {len(methods)}"]
    if async_count:
        lines.append(f"Async methods: {async_count}")
    if bases:
        lines.append(f"Bases: {', '.join(bases)}")
    if decorators:
        lines.append(f"Decorators: {', '.join(decorators)}")
    return lines, bases, decorators


# ─── Python Docs ─────────────────────────────────────────────────────────────

def _auto_describe_callable(name: str, summary_lines: list, is_async: bool = False, docstring: str = "") -> str:
    """Build a minimal auto-description from AST summary when LLM is unavailable."""
    if docstring:
        # Use first sentence of existing docstring
        first_sentence = docstring.split('.')[0].strip()
        return first_sentence + '.' if first_sentence and not first_sentence.endswith('.') else first_sentence
    parts = []
    for line in summary_lines:
        if line.startswith("Calls:"):
            calls = line.replace("Calls: ", "")
            parts.append(f"calls {calls}")
        elif line.startswith("Returns:"):
            parts.append("returns a value")
        elif line.startswith("Raises:"):
            parts.append("may raise exceptions")
    prefix = "async function" if is_async else "function"
    desc = f"`{name}` is a {prefix}"
    if parts:
        desc += " that " + ", ".join(parts[:3])
    return desc + "."


def _extract_python_info(file_path):
    """Extract classes, functions, and docstrings from a Python file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
    except (SyntaxError, Exception):
        return None

    module_doc = ast.get_docstring(tree) or ""
    line_count, non_empty_lines = _count_lines(source)
    imports = _collect_python_imports(tree)
    globals_found = _collect_python_globals(tree)
    comment_lines = _count_comment_lines(source, _comment_prefixes_for_lang("python"))
    classes = []
    functions = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            method_items = []
            cls_info = {
                "name": node.name,
                "docstring": ast.get_docstring(node) or "",
                "methods": [],
                "line": node.lineno,
                "bases": [],
                "decorators": [],
                # Class-level snippet: up to 50 lines covering the header and attribute definitions
                "snippet": _snippet_from_node(source, node, max_lines=50),
            }
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    # Private: starts with _ but is NOT a dunder (__init__, etc.)
                    _m_private = item.name.startswith('_') and not (
                        item.name.startswith('__') and item.name.endswith('__')
                    )
                    # Deprecated: any decorator whose name contains 'deprecated'
                    _m_deprecated = any(
                        (isinstance(d, ast.Name) and 'deprecated' in d.id.lower())
                        or (isinstance(d, ast.Attribute) and 'deprecated' in d.attr.lower())
                        or (isinstance(d, ast.Call) and (
                            (isinstance(d.func, ast.Name) and 'deprecated' in d.func.id.lower())
                            or (isinstance(d.func, ast.Attribute) and 'deprecated' in d.func.attr.lower())
                        ))
                        for d in item.decorator_list
                    )
                    # Extract raised exception class names
                    _m_raises = []
                    for _rc in ast.walk(item):
                        if isinstance(_rc, ast.Raise) and _rc.exc is not None:
                            if isinstance(_rc.exc, ast.Call) and isinstance(_rc.exc.func, ast.Name):
                                if _rc.exc.func.id not in _m_raises:
                                    _m_raises.append(_rc.exc.func.id)
                            elif isinstance(_rc.exc, ast.Name) and _rc.exc.id not in _m_raises:
                                _m_raises.append(_rc.exc.id)
                    method_info = {
                        "name": item.name,
                        "args": args,
                        "docstring": ast.get_docstring(item) or "",
                        "line": item.lineno,
                        "is_async": isinstance(item, ast.AsyncFunctionDef),
                        "is_private": _m_private,
                        "is_deprecated": _m_deprecated,
                        "raises": _m_raises,
                        "summary": _summarize_python_callable(item),
                        "signature": _format_python_signature(item),
                        "snippet": _snippet_from_node(source, item),
                    }
                    cls_info["methods"].append(method_info)
                    method_items.append(method_info)
            class_summary, bases, decorators = _summarize_python_class(node, method_items)
            cls_info["summary"] = class_summary
            cls_info["bases"] = bases
            cls_info["decorators"] = decorators
            classes.append(cls_info)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            _f_private = node.name.startswith('_') and not (
                node.name.startswith('__') and node.name.endswith('__')
            )
            _f_deprecated = any(
                (isinstance(d, ast.Name) and 'deprecated' in d.id.lower())
                or (isinstance(d, ast.Attribute) and 'deprecated' in d.attr.lower())
                or (isinstance(d, ast.Call) and (
                    (isinstance(d.func, ast.Name) and 'deprecated' in d.func.id.lower())
                    or (isinstance(d.func, ast.Attribute) and 'deprecated' in d.func.attr.lower())
                ))
                for d in node.decorator_list
            )
            _f_raises = []
            for _rc in ast.walk(node):
                if isinstance(_rc, ast.Raise) and _rc.exc is not None:
                    if isinstance(_rc.exc, ast.Call) and isinstance(_rc.exc.func, ast.Name):
                        if _rc.exc.func.id not in _f_raises:
                            _f_raises.append(_rc.exc.func.id)
                    elif isinstance(_rc.exc, ast.Name) and _rc.exc.id not in _f_raises:
                        _f_raises.append(_rc.exc.id)
            functions.append({
                "name": node.name,
                "args": args,
                "docstring": ast.get_docstring(node) or "",
                "line": node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "is_private": _f_private,
                "is_deprecated": _f_deprecated,
                "raises": _f_raises,
                "summary": _summarize_python_callable(node),
                "signature": _format_python_signature(node),
                "snippet": _snippet_from_node(source, node),
            })

    return {
        "module_docstring": module_doc,
        "classes": classes,
        "functions": functions,
        "line_count": line_count,
        "non_empty_lines": non_empty_lines,
        "imports": imports,
        "globals": globals_found,
        "comment_lines": comment_lines,
    }


def _detect_python_cli(file_path):
    """Detect if a Python file has CLI entry points (argparse/click/typer)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return None

    cli_type = None
    if "argparse" in content:
        cli_type = "argparse"
    elif "import click" in content or "from click" in content:
        cli_type = "click"
    elif "import typer" in content or "from typer" in content:
        cli_type = "typer"

    if cli_type and ("__main__" in content or "if __name__" in content):
        return cli_type
    return None


def generate_python_docs(project_path, files, output_dir):
    """Generate documentation for Python files with LLM enhancement."""
    docs = []
    cli_files = []
    module_summaries = []  # collected for architecture doc

    # Get LLM helper
    llm = get_llm_helper()
    if llm:
        print("  🤖 LLM-enhanced documentation enabled (quality-first mode)")

    for rel_path in files:
        full_path = Path(project_path) / rel_path
        info = _extract_python_info(full_path)
        if info is None:
            continue

        cli_type = _detect_python_cli(full_path)
        if cli_type:
            cli_files.append({"path": rel_path, "cli_type": cli_type})

        # Build file-context string passed to every LLM call for this file
        file_context = _build_file_context(info, rel_path)

        doc_content = f"# {rel_path}\n\n"

        # ── Module overview (holistic full-file read) ────────────────────────
        if llm:
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as _f:
                    _full_src = _f.read()
                _overview = llm.generate_module_overview(_full_src, "python", rel_path)
                if _overview:
                    doc_content += "## Module Overview\n\n"
                    doc_content += _overview.strip() + "\n\n---\n\n"
                    # Save summary text for architecture doc
                    module_summaries.append({"path": rel_path, "summary": _overview[:1200]})
            except Exception as _e:
                print(f"  ⚠ Module overview failed for {rel_path}: {_e}")
        elif info["module_docstring"]:
            doc_content += f"{info['module_docstring']}\n\n"

        summary_lines = [
            f"Lines: {info.get('line_count', 0)} ({info.get('non_empty_lines', 0)} non-empty)",
            f"Classes: {len(info['classes'])}",
            f"Functions: {len(info['functions'])}",
            f"Comment lines: {info.get('comment_lines', 0)}",
        ]
        if info.get("imports"):
            imports = info["imports"]
            import_text = ", ".join(imports[:8])
            if len(imports) > 8:
                import_text += " ..."
            summary_lines.append(f"Imports: {import_text}")
        if info.get("globals"):
            globals_text = ", ".join(info["globals"][:8])
            if len(info["globals"]) > 8:
                globals_text += " ..."
            summary_lines.append(f"Globals: {globals_text}")
        if cli_type:
            summary_lines.append(f"CLI: {cli_type}")
        doc_content += _format_summary_block(summary_lines, level=2)

        # Classes
        for cls in info["classes"]:
            bases_str = ""
            if cls.get("bases"):
                bases_str = f" ({', '.join(cls['bases'])})"
            doc_content += f"## class `{cls['name']}`{bases_str}\n\n"

            # LLM-enhanced class description (with file context)
            _cls_described = False
            if llm and cls.get("snippet"):
                llm_docs = llm.generate_class_docs(
                    cls["snippet"], "python", file_context=file_context
                )
                if llm_docs:
                    _cls_described = True
                    doc_content += f"{llm_docs.get('description', '')}\n\n"
                    if llm_docs.get('attributes'):
                        doc_content += "**Attributes:**\n\n"
                        doc_content += "| Attribute | Description |\n|-----------|-------------|\n"
                        for attr, desc in llm_docs['attributes'].items():
                            doc_content += f"| `{attr}` | {desc} |\n"
                        doc_content += "\n"
                    if llm_docs.get('notes'):
                        doc_content += f"> **Note:** {llm_docs['notes']}\n\n"

            # Fall back to docstring or auto-description
            if not _cls_described:
                if cls["docstring"]:
                    doc_content += f"{cls['docstring']}\n\n"
                elif cls.get("summary"):
                    auto_desc = _auto_describe_callable(cls["name"], cls["summary"], docstring="")
                    doc_content += f"{auto_desc}\n\n"

            doc_content += _format_summary_block(cls.get("summary", []), level=3)
            if cls.get("snippet") and not _cls_described:
                doc_content += "#### Definition Snippet\n\n"
                doc_content += "```python\n"
                doc_content += f"{cls['snippet']}\n"
                doc_content += "```\n\n"

            for method in cls["methods"]:
                async_prefix = "async " if method["is_async"] else ""
                args_str = ", ".join(method["args"])
                signature = method.get("signature") or f"{method['name']}({args_str})"
                doc_content += f"### {async_prefix}`{signature}`\n\n"
                if method.get("is_private"):
                    doc_content += "> 🔒 **Internal API** — not part of the public interface. May change without notice.\n\n"
                if method.get("is_deprecated"):
                    doc_content += "> ⚠️ **Deprecated** — This method is marked deprecated. Check source for the recommended alternative.\n\n"

                # LLM-enhanced method description (with file context)
                _meth_described = False
                if llm and method.get("snippet"):
                    llm_docs = llm.generate_function_docs(
                        method["snippet"], "python", file_context=file_context
                    )
                    if llm_docs:
                        _meth_described = True
                        doc_content += f"{llm_docs.get('description', '')}\n\n"
                        if llm_docs.get('parameters'):
                            doc_content += "**Parameters:**\n\n"
                            doc_content += "| Parameter | Description |\n|-----------|-------------|\n"
                            for param, desc in llm_docs['parameters'].items():
                                doc_content += f"| `{param}` | {desc} |\n"
                            doc_content += "\n"
                        if llm_docs.get('returns'):
                            doc_content += f"**Returns:** {llm_docs['returns']}\n\n"
                        if llm_docs.get('notes'):
                            doc_content += f"> **Note:** {llm_docs['notes']}\n\n"

                # Fall back to docstring or auto-description
                if not _meth_described:
                    if method["docstring"]:
                        doc_content += f"{method['docstring']}\n\n"
                    elif method.get("summary"):
                        auto_desc = _auto_describe_callable(
                            method["name"], method["summary"],
                            is_async=method["is_async"], docstring=""
                        )
                        doc_content += f"{auto_desc}\n\n"

                if method.get("raises"):
                    doc_content += "**Raises:** " + ", ".join(f"`{r}`" for r in method["raises"]) + "\n\n"

                doc_content += _format_summary_block(method.get("summary", []), level=4)
                if method.get("snippet") and not _meth_described:
                    doc_content += "#### Code Snippet\n\n"
                    doc_content += "```python\n"
                    doc_content += f"{method['snippet']}\n"
                    doc_content += "```\n\n"

        # Top-level functions
        for func in info["functions"]:
            async_prefix = "async " if func["is_async"] else ""
            args_str = ", ".join(func["args"])
            signature = func.get("signature") or f"{func['name']}({args_str})"
            doc_content += f"## {async_prefix}`{signature}`\n\n"
            if func.get("is_private"):
                doc_content += "> 🔒 **Internal API** — not part of the public interface. May change without notice.\n\n"
            if func.get("is_deprecated"):
                doc_content += "> ⚠️ **Deprecated** — This function is marked deprecated. Check source for the recommended alternative.\n\n"

            # LLM-enhanced function description (with file context)
            _func_described = False
            if llm and func.get("snippet"):
                llm_docs = llm.generate_function_docs(
                    func["snippet"], "python", file_context=file_context
                )
                if llm_docs:
                    _func_described = True
                    doc_content += f"{llm_docs.get('description', '')}\n\n"
                    if llm_docs.get('parameters'):
                        doc_content += "**Parameters:**\n\n"
                        doc_content += "| Parameter | Description |\n|-----------|-------------|\n"
                        for param, desc in llm_docs['parameters'].items():
                            doc_content += f"| `{param}` | {desc} |\n"
                        doc_content += "\n"
                    if llm_docs.get('returns'):
                        doc_content += f"**Returns:** {llm_docs['returns']}\n\n"
                    if llm_docs.get('notes'):
                        doc_content += f"> **Note:** {llm_docs['notes']}\n\n"

            # Fall back to docstring or auto-description
            if not _func_described:
                if func["docstring"]:
                    doc_content += f"{func['docstring']}\n\n"
                elif func.get("summary"):
                    auto_desc = _auto_describe_callable(
                        func["name"], func["summary"],
                        is_async=func["is_async"], docstring=""
                    )
                    doc_content += f"{auto_desc}\n\n"

            if func.get("raises"):
                doc_content += "**Raises:** " + ", ".join(f"`{r}`" for r in func["raises"]) + "\n\n"

            doc_content += _format_summary_block(func.get("summary", []), level=3)
            if func.get("snippet") and not _func_described:
                doc_content += "### Code Snippet\n\n"
                doc_content += "```python\n"
                doc_content += f"{func['snippet']}\n"
                doc_content += "```\n\n"

        docs.append({"path": rel_path, "content": doc_content, "info": info})

    # Write Python API docs
    py_dir = output_dir / "api" / "python"
    py_dir.mkdir(parents=True, exist_ok=True)

    index_content = "# Python API Reference\n\n"
    index_content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    index_content += f"**{len(files)} Python files detected**\n\n"

    total_classes = 0
    total_functions = 0
    total_documented = 0
    total_undocumented = 0

    for doc in docs:
        safe_name = doc["path"].replace("/", "_").replace("\\", "_").replace(".py", "")
        doc_file = py_dir / f"{safe_name}.md"
        with open(doc_file, "w", encoding="utf-8") as f:
            f.write(doc["content"])
        index_content += f"- [{doc['path']}]({safe_name}.md)\n"

        info = doc["info"]
        total_classes += len(info["classes"])
        for cls in info["classes"]:
            for m in cls["methods"]:
                if m["docstring"]:
                    total_documented += 1
                else:
                    total_undocumented += 1
        for func in info["functions"]:
            total_functions += 1
            if func["docstring"]:
                total_documented += 1
            else:
                total_undocumented += 1

    total_symbols = total_documented + total_undocumented
    coverage = (total_documented / total_symbols * 100) if total_symbols > 0 else 0

    index_content += f"\n---\n"
    index_content += f"### Coverage Summary\n"
    index_content += f"- Classes: {total_classes}\n"
    index_content += f"- Functions: {total_functions}\n"
    index_content += f"- Documented: {total_documented}/{total_symbols} ({coverage:.0f}%)\n"

    with open(py_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(index_content)

    # Write CLI docs
    if cli_files:
        cli_dir = output_dir / "cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        cli_content = "# CLI Reference\n\n"
        cli_content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
        for cli in cli_files:
            cli_content += f"## `{cli['path']}`\n\n"
            cli_content += f"- CLI framework: **{cli['cli_type']}**\n"
            cli_content += f"- Run: `python {cli['path']} --help`\n\n"

        with open(cli_dir / "python_cli.md", "w", encoding="utf-8") as f:
            f.write(cli_content)

    return {
        "files": len(files),
        "classes": total_classes,
        "functions": total_functions,
        "documented": total_documented,
        "undocumented": total_undocumented,
        "coverage": round(coverage, 1),
        "cli_files": len(cli_files),
        "module_summaries": module_summaries,
    }


# ─── JavaScript / TypeScript Docs ────────────────────────────────────────────

def _extract_jsdoc_blocks(file_path):
    """Extract JSDoc comment blocks and associated function/class names."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    blocks = []

    # Match JSDoc blocks: /** ... */
    jsdoc_pattern = re.compile(r'/\*\*(.*?)\*/', re.DOTALL)
    # Match function/class/const after JSDoc
    decl_pattern = re.compile(
        r'(?:export\s+)?(?:default\s+)?'
        r'(?:(async\s+)?function\s+(\w+)|class\s+(\w+)|'
        r'(?:const|let|var)\s+(\w+)\s*=)',
        re.MULTILINE
    )

    for match in jsdoc_pattern.finditer(content):
        doc_text = match.group(1).strip()
        # Clean up leading * from each line
        lines = []
        for line in doc_text.split("\n"):
            line = re.sub(r'^\s*\*\s?', '', line)
            lines.append(line)
        cleaned_doc = "\n".join(lines).strip()

        # Find next declaration after this JSDoc
        after = content[match.end():]
        decl_match = decl_pattern.match(after.lstrip())
        name = "unknown"
        kind = "symbol"
        if decl_match:
            if decl_match.group(2):
                name = decl_match.group(2)
                kind = "function"
            elif decl_match.group(3):
                name = decl_match.group(3)
                kind = "class"
            elif decl_match.group(4):
                name = decl_match.group(4)
                kind = "const"

        blocks.append({"name": name, "kind": kind, "doc": cleaned_doc})

    # Also find exports/functions without JSDoc
    all_decls = decl_pattern.finditer(content)
    documented_names = {b["name"] for b in blocks}
    for m in all_decls:
        name = m.group(2) or m.group(3) or m.group(4)
        if name and name not in documented_names:
            kind = "function" if m.group(2) else ("class" if m.group(3) else "const")
            blocks.append({"name": name, "kind": kind, "doc": ""})

    return blocks


def _detect_react_components(file_path):
    """Detect React components in a JS/JSX/TSX file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    components = []
    # Match function components
    pattern = re.compile(
        r'(?:export\s+(?:default\s+)?)?(?:const|function)\s+(\w+)\s*(?:=\s*(?:\([^)]*\)|[^=])\s*=>|[({])',
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        name = m.group(1)
        if name and name[0].isupper():  # React components are PascalCase
            # Try to find props type
            props_match = re.search(rf'{name}\s*(?::\s*React\.FC<(\w+)>|\((?:\{{[^}}]*\}}|(\w+)))', content)
            props_type = ""
            if props_match:
                props_type = props_match.group(1) or props_match.group(2) or ""
            components.append({"name": name, "props_type": props_type})

    return components


def _extract_js_function_body(content: str, name: str, max_lines: int = 150) -> str:
    """Extract the full source of a JS/TS function/class by name.

    Walks the source character-by-character from the first occurrence of the
    name to match balanced braces, giving the LLM the complete implementation.
    """
    # Find the start of the declaration
    pattern = re.compile(
        rf'(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+{re.escape(name)}\s*\(|'
        rf'(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=])\s*=>|'
        rf'class\s+{re.escape(name)}\s*(?:extends\s+\w+\s*)?{{)'
    )
    m = pattern.search(content)
    if not m:
        # Plain match fallback
        idx = content.find(name)
        if idx == -1:
            return ""
        start = max(0, idx - 30)
    else:
        start = m.start()

    # Walk forward to collect balanced braces
    brace_depth = 0
    paren_depth = 0
    in_string = None
    i = start
    lines_seen = 0
    max_chars = max_lines * 120  # rough estimate
    body_end = min(start + max_chars, len(content))

    for i in range(start, body_end):
        ch = content[i]
        if ch == '\n':
            lines_seen += 1
            if lines_seen >= max_lines and brace_depth == 0:
                break
        if in_string:
            if ch == in_string and (i == 0 or content[i - 1] != '\\'):
                in_string = None
            continue
        if ch in ('"', "'", '`'):
            in_string = ch
            continue
        if ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth <= 0:
                i += 1
                break
        elif ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1

    return content[start:i].strip()
    """Generate documentation for JavaScript/TypeScript files."""
    all_files = js_files + ts_files
    lang_label = "JavaScript/TypeScript"

    js_dir = output_dir / "api" / "javascript"
    js_dir.mkdir(parents=True, exist_ok=True)

    llm = get_llm_helper()
    if llm:
        print("  🤖 LLM-enhanced JS/TS documentation enabled (quality-first mode)")

    module_summaries = []

    index_content = f"# {lang_label} API Reference\n\n"
    index_content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    index_content += f"**{len(all_files)} files detected**\n\n"

    total_symbols = 0
    total_documented = 0
    component_count = 0

    for rel_path in all_files:
        full_path = Path(project_path) / rel_path
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                js_content = f.read()
        except Exception:
            js_content = ""
        blocks = _extract_jsdoc_blocks(full_path)
        components = _detect_react_components(full_path)

        # Build file-level context string
        _lang = "typescript" if rel_path.endswith((".ts", ".tsx")) else "javascript"
        _import_lines = [l.strip() for l in js_content.splitlines() if l.strip().startswith("import ")][:12]
        _export_names = re.findall(r'export\s+(?:default\s+)?(?:const|function|class|async function)\s+(\w+)', js_content)
        file_context_parts = [f"File: {rel_path}"]
        if _import_lines:
            file_context_parts.append("Imports: " + "; ".join(_import_lines[:8]))
        if _export_names:
            file_context_parts.append("Exports: " + ", ".join(_export_names[:10]))
        if components:
            file_context_parts.append("React components: " + ", ".join(c["name"] for c in components[:8]))
        file_context = "\n".join(file_context_parts)

        doc_content = f"# {rel_path}\n\n"
        line_count, non_empty_lines = _count_lines(js_content)
        comment_lines = _count_comment_lines(js_content, _comment_prefixes_for_lang("javascript"))

        # ── Module overview (holistic full-file read) ────────────────────────
        if llm and js_content.strip():
            try:
                _overview = llm.generate_module_overview(js_content, _lang, rel_path)
                if _overview:
                    doc_content += "## Module Overview\n\n"
                    doc_content += _overview.strip() + "\n\n---\n\n"
                    module_summaries.append({"path": rel_path, "summary": _overview[:1200]})
            except Exception as _e:
                print(f"  ⚠ Module overview failed for {rel_path}: {_e}")

        summary_lines = [
            f"Lines: {line_count} ({non_empty_lines} non-empty)",
            f"Symbols: {len(blocks)}",
        ]
        if components:
            summary_lines.append(f"React components: {len(components)}")
        if comment_lines:
            summary_lines.append(f"Comment lines: {comment_lines}")
        doc_content += _format_summary_block(summary_lines, level=2)

        if components:
            component_count += len(components)
            doc_content += "## React Components\n\n"
            for comp in components:
                doc_content += f"### `<{comp['name']} />`\n\n"
                if comp["props_type"]:
                    doc_content += f"- Props: `{comp['props_type']}`\n\n"

        # ── Pre-compute LLM docs for all undocumented public symbols in one
        #    batch call — more accurate than snippet-by-snippet ────────────────
        _undoc_names = [b["name"] for b in blocks if not b["doc"] and not b["name"].startswith("_")]
        _batch_docs: dict = {}
        if llm and _undoc_names and js_content.strip():
            try:
                _batch_docs = llm.generate_file_symbol_docs_batch(
                    js_content, _lang, rel_path, _undoc_names
                ) or {}
            except Exception as _e:
                print(f"  ⚠ Batch symbol docs failed for {rel_path}: {_e}")

        for block in blocks:
            total_symbols += 1
            _js_private = block['name'].startswith('_') and not (
                block['name'].startswith('__') and block['name'].endswith('__')
            )
            prefix = "async " if "async" in block.get("kind", "") else ""
            doc_content += f"## {block['kind']} `{prefix}{block['name']}`\n\n"
            if _js_private:
                doc_content += "> 🔒 **Internal** — not part of the public API. May change without notice.\n\n"

            if block["doc"]:
                total_documented += 1
                doc_content += f"{block['doc']}\n\n"
            elif not _js_private and _batch_docs.get(block["name"]):
                total_documented += 1
                doc_content += f"{_batch_docs[block['name']]}\n\n"
            elif llm and not _js_private:
                # Per-symbol fallback: find function body properly
                _fn_body = _extract_js_function_body(js_content, block["name"])
                if _fn_body:
                    _js_llm = llm.generate_function_docs(_fn_body, _lang, file_context=file_context)
                    if _js_llm and _js_llm.get("description"):
                        total_documented += 1
                        doc_content += f"{_js_llm['description']}\n\n"
                        if _js_llm.get('parameters'):
                            doc_content += "**Parameters:**\n\n"
                            doc_content += "| Parameter | Description |\n|-----------|-------------|\n"
                            for _p, _d in _js_llm['parameters'].items():
                                doc_content += f"| `{_p}` | {_d} |\n"
                            doc_content += "\n"
                        if _js_llm.get('returns'):
                            doc_content += f"**Returns:** {_js_llm['returns']}\n\n"
                        if _js_llm.get('notes'):
                            doc_content += f"> **Note:** {_js_llm['notes']}\n\n"
                    else:
                        doc_content += "*No documentation available.*\n\n"
                else:
                    doc_content += "*No documentation available.*\n\n"
            else:
                doc_content += "*No documentation available.*\n\n"

        safe_name = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
        doc_file = js_dir / f"{safe_name}.md"
        with open(doc_file, "w", encoding="utf-8") as f:
            f.write(doc_content)
        index_content += f"- [{rel_path}]({safe_name}.md)\n"

    coverage = (total_documented / total_symbols * 100) if total_symbols > 0 else 0
    index_content += f"\n---\n"
    index_content += f"### Coverage Summary\n"
    index_content += f"- Symbols: {total_symbols}\n"
    index_content += f"- React Components: {component_count}\n"
    index_content += f"- Documented: {total_documented}/{total_symbols} ({coverage:.0f}%)\n"

    with open(js_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(index_content)

    return {
        "files": len(all_files),
        "symbols": total_symbols,
        "documented": total_documented,
        "coverage": round(coverage, 1),
        "react_components": component_count,
        "module_summaries": module_summaries,
    }


# ─── Generic Docs (Java, C#, Go, Rust, C/C++, PHP, Ruby, etc.) ──────────────

def _extract_doc_comments(file_path, lang):
    """Extract documentation comments from various languages."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    symbols = []

    if lang in ("java", "csharp", "php", "kotlin", "scala", "dart"):
        # Javadoc-style /** ... */
        pattern = re.compile(r'/\*\*(.*?)\*/\s*(?:(?:public|private|protected|internal|static|abstract|final|override|suspend|fun|class|interface|enum|struct|val|var)\s+)*(?:class|interface|enum|struct|fun|def|function|void|int|string|bool|var|val|object)?\s*(\w+)', re.DOTALL)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*\*\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "go":
        # Go doc comments: // lines before func/type
        pattern = re.compile(r'((?://[^\n]*\n)+)\s*(?:func|type|var|const)\s+(?:\([^)]*\)\s+)?(\w+)', re.MULTILINE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*//\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "rust":
        # Rust doc comments: /// lines before fn/struct/enum/impl
        pattern = re.compile(r'((?:///[^\n]*\n)+)\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl|type|const|static|mod)\s+(\w+)', re.MULTILINE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*///\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang in ("c", "cpp"):
        # Doxygen-style /** ... */ or /// before declarations
        pattern = re.compile(r'/\*\*(.*?)\*/\s*(?:(?:static|extern|inline|virtual|const|unsigned|signed)\s+)*(?:void|int|char|float|double|bool|auto|class|struct|enum|typedef)\s+(\w+)', re.DOTALL)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*\*\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "ruby":
        # Ruby: # comments before def/class/module
        pattern = re.compile(r'((?:#[^\n]*\n)+)\s*(?:def|class|module)\s+(\w+)', re.MULTILINE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*#\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "swift":
        # Swift: /// comments before func/class/struct/enum
        pattern = re.compile(r'((?:///[^\n]*\n)+)\s*(?:public\s+|private\s+|internal\s+|open\s+)?(?:func|class|struct|enum|protocol)\s+(\w+)', re.MULTILINE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*///\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "shell":
        # Shell: # comments before function
        pattern = re.compile(r'((?:#[^\n]*\n)+)\s*(?:function\s+)?(\w+)\s*\(\)', re.MULTILINE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*#\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    elif lang == "sql":
        # SQL: -- comments before CREATE
        pattern = re.compile(r'((?:--[^\n]*\n)+)\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|TRIGGER|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`|\[|")?(\w+)', re.MULTILINE | re.IGNORECASE)
        for m in pattern.finditer(content):
            doc = re.sub(r'^\s*--\s?', '', m.group(1), flags=re.MULTILINE).strip()
            symbols.append({"name": m.group(2), "doc": doc})

    # Find undocumented declarations too
    if lang == "java":
        all_decls = re.findall(r'(?:public|private|protected)\s+(?:static\s+)?(?:class|interface|enum|void|int|String|boolean|long|double|float)\s+(\w+)', content)
    elif lang == "go":
        all_decls = re.findall(r'(?:func|type)\s+(?:\([^)]*\)\s+)?(\w+)', content)
    elif lang == "rust":
        all_decls = re.findall(r'(?:pub\s+)?(?:fn|struct|enum|trait)\s+(\w+)', content)
    elif lang in ("c", "cpp"):
        all_decls = re.findall(r'(?:void|int|char|float|double|bool|auto|class|struct)\s+(\w+)\s*[({]', content)
    elif lang == "ruby":
        all_decls = re.findall(r'(?:def|class|module)\s+(\w+)', content)
    elif lang == "swift":
        all_decls = re.findall(r'(?:func|class|struct|enum|protocol)\s+(\w+)', content)
    elif lang == "sql":
        all_decls = re.findall(r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`|\[|")?(\w+)', content, re.IGNORECASE)
    else:
        all_decls = []

    documented_names = {s["name"] for s in symbols}
    for name in all_decls:
        if name not in documented_names:
            symbols.append({"name": name, "doc": ""})

    return symbols


def _find_symbol_in_source(content: str, name: str, lang: str, max_lines: int = 100) -> str:
    """Extract a symbol body from source for any language, for the LLM fallback.

    Returns up to max_lines lines starting from the declaration of `name`.
    """
    lines = content.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        # Match common patterns across languages
        if re.search(rf'\b{re.escape(name)}\b', line):
            start_idx = i
            break
    if start_idx is None:
        return ""
    end_idx = min(start_idx + max_lines, len(lines))
    return "\n".join(lines[start_idx:end_idx])


def generate_generic_docs(project_path, lang, files, output_dir):
    """Generate documentation for any supported language, with full LLM enhancement."""
    lang_dir = output_dir / "api" / lang
    lang_dir.mkdir(parents=True, exist_ok=True)

    lang_labels = {
        "java": "Java", "csharp": "C#", "go": "Go", "rust": "Rust",
        "c": "C", "cpp": "C++", "php": "PHP", "ruby": "Ruby",
        "swift": "Swift", "kotlin": "Kotlin", "sql": "SQL",
        "dart": "Dart", "shell": "Shell/Bash", "r": "R",
        "lua": "Lua", "scala": "Scala", "elixir": "Elixir",
    }
    label = lang_labels.get(lang, lang.capitalize())

    llm = get_llm_helper()
    if llm:
        print(f"  🤖 LLM-enhanced {label} documentation enabled (quality-first mode)")

    module_summaries = []

    index_content = f"# {label} API Reference\n\n"
    index_content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    index_content += f"**{len(files)} files detected**\n\n"

    total_symbols = 0
    total_documented = 0

    for rel_path in files:
        full_path = Path(project_path) / rel_path
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                file_content = f.read()
        except Exception:
            file_content = ""
        symbols = _extract_doc_comments(full_path, lang)

        doc_content = f"# {rel_path}\n\n"
        line_count, non_empty_lines = _count_lines(file_content)
        comment_lines = _count_comment_lines(file_content, _comment_prefixes_for_lang(lang))
        summary_lines = [
            f"Lines: {line_count} ({non_empty_lines} non-empty)",
            f"Symbols: {len(symbols)}",
        ]
        if comment_lines:
            summary_lines.append(f"Comment lines: {comment_lines}")
        doc_content += _format_summary_block(summary_lines, level=2)

        # ── Module overview (holistic full-file read) ────────────────────────
        if llm and file_content.strip():
            try:
                _overview = llm.generate_module_overview(file_content, lang, rel_path)
                if _overview:
                    doc_content += "## Module Overview\n\n"
                    doc_content += _overview.strip() + "\n\n---\n\n"
                    module_summaries.append({"path": rel_path, "summary": _overview[:1200]})
            except Exception as _e:
                print(f"  ⚠ Module overview failed for {rel_path}: {_e}")

        # ── Batch LLM docs for undocumented symbols ──────────────────────────
        _undoc_names = [s["name"] for s in symbols if not s["doc"]]
        _batch_docs: dict = {}
        if llm and _undoc_names and file_content.strip():
            try:
                _batch_docs = llm.generate_file_symbol_docs_batch(
                    file_content, lang, rel_path, _undoc_names
                ) or {}
            except Exception as _e:
                print(f"  ⚠ Batch symbol docs failed for {rel_path}: {_e}")

        for sym in symbols:
            total_symbols += 1
            doc_content += f"## `{sym['name']}`\n\n"
            if sym["doc"]:
                total_documented += 1
                doc_content += f"{sym['doc']}\n\n"
            elif _batch_docs.get(sym["name"]):
                total_documented += 1
                doc_content += f"{_batch_docs[sym['name']]}\n\n"
            elif llm and file_content.strip():
                # Per-symbol fallback: find symbol body in source
                _sym_body = _find_symbol_in_source(file_content, sym["name"], lang)
                if _sym_body:
                    _sym_docs = llm.generate_symbol_docs(
                        sym["name"], _sym_body, lang, file_context=f"File: {rel_path}"
                    )
                    if _sym_docs:
                        total_documented += 1
                        doc_content += f"{_sym_docs}\n\n"
                    else:
                        doc_content += "*No documentation available.*\n\n"
                else:
                    doc_content += "*No documentation available.*\n\n"
            else:
                doc_content += "*No documentation available.*\n\n"

        safe_name = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
        doc_file = lang_dir / f"{safe_name}.md"
        with open(doc_file, "w", encoding="utf-8") as f:
            f.write(doc_content)
        index_content += f"- [{rel_path}]({safe_name}.md)\n"

    coverage = (total_documented / total_symbols * 100) if total_symbols > 0 else 0
    index_content += f"\n---\n"
    index_content += f"### Coverage Summary\n"
    index_content += f"- Symbols: {total_symbols}\n"
    index_content += f"- Documented: {total_documented}/{total_symbols} ({coverage:.0f}%)\n"

    with open(lang_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(index_content)

    return {
        "files": len(files),
        "symbols": total_symbols,
        "documented": total_documented,
        "coverage": round(coverage, 1),
        "module_summaries": module_summaries,
    }


# ─── Android XML Docs ────────────────────────────────────────────────────────

def _parse_android_layout(file_path):
    """Extract UI elements from Android layout XML."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
    except Exception:
        return []

    elements = []
    for elem in root.iter():
        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        elem_id = elem.get('{http://schemas.android.com/apk/res/android}id', '')
        if elem_id:
            elem_id = elem_id.replace('@+id/', '').replace('@id/', '')
        text = elem.get('{http://schemas.android.com/apk/res/android}text', '')
        elements.append({"tag": tag, "id": elem_id, "text": text})
    return elements


def _parse_android_manifest(file_path):
    """Extract permissions, activities, services from AndroidManifest.xml."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
    except Exception:
        return {}

    ns = {'android': 'http://schemas.android.com/apk/res/android'}
    permissions = [p.get('{http://schemas.android.com/apk/res/android}name', '') for p in root.findall('.//uses-permission')]
    activities = [a.get('{http://schemas.android.com/apk/res/android}name', '') for a in root.findall('.//activity')]
    services = [s.get('{http://schemas.android.com/apk/res/android}name', '') for s in root.findall('.//service')]
    receivers = [r.get('{http://schemas.android.com/apk/res/android}name', '') for r in root.findall('.//receiver')]

    return {
        "permissions": permissions,
        "activities": activities,
        "services": services,
        "receivers": receivers
    }


def _parse_android_strings(file_path):
    """Extract string resources from strings.xml."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
    except Exception:
        return []

    strings = []
    for string_elem in root.findall('.//string'):
        name = string_elem.get('name', '')
        value = string_elem.text or ''
        strings.append({"name": name, "value": value})
    return strings


def generate_xml_docs(project_path, files, output_dir):
    """Generate documentation for Android XML files."""
    xml_dir = output_dir / "api" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    index_content = "# Android XML Resources\n\n"
    index_content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
    index_content += f"**{len(files)} XML files detected**\n\n"

    layout_files = []
    manifest_files = []
    resource_files = []
    other_files = []

    for rel_path in files:
        if 'layout' in rel_path:
            layout_files.append(rel_path)
        elif 'AndroidManifest.xml' in rel_path:
            manifest_files.append(rel_path)
        elif 'values' in rel_path:
            resource_files.append(rel_path)
        else:
            other_files.append(rel_path)

    # Layout files
    if layout_files:
        index_content += "## Layout Files\n\n"
        for rel_path in layout_files:
            full_path = Path(project_path) / rel_path
            elements = _parse_android_layout(full_path)
            doc_content = f"# {rel_path}\n\n"
            doc_content += f"## Summary\n\n- Elements: {len(elements)}\n\n"
            if elements:
                doc_content += "## UI Elements\n\n"
                doc_content += "| Tag | ID | Text |\n|---|---|---|\n"
                for elem in elements[:50]:
                    doc_content += f"| {elem['tag']} | {elem['id']} | {elem['text'][:30] if elem['text'] else ''} |\n"
            safe_name = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
            doc_file = xml_dir / f"{safe_name}.md"
            with open(doc_file, "w", encoding="utf-8") as f:
                f.write(doc_content)
            index_content += f"- [{rel_path}]({safe_name}.md)\n"

    # Manifest files
    if manifest_files:
        index_content += "\n## Manifest Files\n\n"
        for rel_path in manifest_files:
            full_path = Path(project_path) / rel_path
            manifest_data = _parse_android_manifest(full_path)
            doc_content = f"# {rel_path}\n\n"
            if manifest_data.get("permissions"):
                doc_content += "## Permissions\n\n"
                for perm in manifest_data["permissions"]:
                    doc_content += f"- `{perm}`\n"
                doc_content += "\n"
            if manifest_data.get("activities"):
                doc_content += "## Activities\n\n"
                for activity in manifest_data["activities"]:
                    doc_content += f"- `{activity}`\n"
                doc_content += "\n"
            if manifest_data.get("services"):
                doc_content += "## Services\n\n"
                for service in manifest_data["services"]:
                    doc_content += f"- `{service}`\n"
                doc_content += "\n"
            if manifest_data.get("receivers"):
                doc_content += "## Receivers\n\n"
                for receiver in manifest_data["receivers"]:
                    doc_content += f"- `{receiver}`\n"
                doc_content += "\n"
            safe_name = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
            doc_file = xml_dir / f"{safe_name}.md"
            with open(doc_file, "w", encoding="utf-8") as f:
                f.write(doc_content)
            index_content += f"- [{rel_path}]({safe_name}.md)\n"

    # Resource files
    if resource_files:
        index_content += "\n## Resource Files\n\n"
        for rel_path in resource_files:
            full_path = Path(project_path) / rel_path
            if 'strings.xml' in rel_path:
                strings = _parse_android_strings(full_path)
                doc_content = f"# {rel_path}\n\n"
                doc_content += f"## String Resources\n\n"
                doc_content += f"Total: {len(strings)}\n\n"
                if strings:
                    doc_content += "| Name | Value |\n|---|---|\n"
                    for s in strings[:100]:
                        value = s['value'][:50] if len(s['value']) > 50 else s['value']
                        doc_content += f"| {s['name']} | {value} |\n"
                safe_name = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
                doc_file = xml_dir / f"{safe_name}.md"
                with open(doc_file, "w", encoding="utf-8") as f:
                    f.write(doc_content)
                index_content += f"- [{rel_path}]({safe_name}.md)\n"

    with open(xml_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(index_content)

    return {
        "files": len(files),
        "layouts": len(layout_files),
        "manifests": len(manifest_files),
        "resources": len(resource_files),
    }


# ─── Dependency Docs ─────────────────────────────────────────────────────────

def generate_dependency_docs(project_path, frameworks, output_dir):
    """Generate dependency documentation from package files."""
    dep_dir = output_dir / "dependencies"
    dep_dir.mkdir(parents=True, exist_ok=True)

    project = Path(project_path)
    content = "# Project Dependencies\n\n"
    content += f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"

    # Python
    req_file = project / "requirements.txt"
    if req_file.exists():
        content += "## Python (requirements.txt)\n\n"
        content += "| Package | Version |\n|---|---|\n"
        with open(req_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "==" in line:
                        pkg, ver = line.split("==", 1)
                        content += f"| {pkg.strip()} | {ver.strip()} |\n"
                    elif ">=" in line:
                        pkg, ver = line.split(">=", 1)
                        content += f"| {pkg.strip()} | >={ver.strip()} |\n"
                    else:
                        content += f"| {line} | latest |\n"
        content += "\n"

    pyproject = project / "pyproject.toml"
    if pyproject.exists():
        content += "## Python (pyproject.toml)\n\n"
        content += f"See [{pyproject.name}](../{pyproject.name}) for full configuration.\n\n"

    # Node.js
    pkg_json = project / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, "r") as f:
                pkg = json.load(f)
            if pkg.get("dependencies"):
                content += "## Node.js Dependencies\n\n"
                content += "| Package | Version |\n|---|---|\n"
                for name, version in pkg["dependencies"].items():
                    content += f"| {name} | {version} |\n"
                content += "\n"
            if pkg.get("devDependencies"):
                content += "## Node.js Dev Dependencies\n\n"
                content += "| Package | Version |\n|---|---|\n"
                for name, version in pkg["devDependencies"].items():
                    content += f"| {name} | {version} |\n"
                content += "\n"
        except Exception:
            pass

    # Go
    go_mod = project / "go.mod"
    if go_mod.exists():
        content += "## Go Modules (go.mod)\n\n"
        with open(go_mod, "r") as f:
            content += f"```\n{f.read()}\n```\n\n"

    # Rust
    cargo_toml = project / "Cargo.toml"
    if cargo_toml.exists():
        content += "## Rust (Cargo.toml)\n\n"
        content += f"See [{cargo_toml.name}](../{cargo_toml.name}) for full configuration.\n\n"

    # Gemfile
    gemfile = project / "Gemfile"
    if gemfile.exists():
        content += "## Ruby (Gemfile)\n\n"
        with open(gemfile, "r") as f:
            content += f"```ruby\n{f.read()}\n```\n\n"

    # Composer
    composer = project / "composer.json"
    if composer.exists():
        content += "## PHP (composer.json)\n\n"
        content += f"See [{composer.name}](../{composer.name}) for full configuration.\n\n"

    with open(dep_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(content)

    return True


# ─── Project Overview ─────────────────────────────────────────────────────────

def generate_overview(project_path, lang_files, frameworks, output_dir):
    """Generate project overview documentation."""
    project = Path(project_path)
    project_name = project.name

    content = f"# {project_name} — Auto-Generated Documentation\n\n"
    content += f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} by FoxNest Auto-Docs*\n\n"

    # README
    readme = project / "README.md"
    if readme.exists():
        with open(readme, "r", encoding="utf-8", errors="ignore") as f:
            readme_content = f.read()
        content += "## Project README\n\n"
        content += readme_content + "\n\n"

    # Languages detected
    content += "---\n\n## Languages Detected\n\n"
    content += "| Language | Files |\n|---|---|\n"
    for lang, files in sorted(lang_files.items()):
        content += f"| {lang.capitalize()} | {len(files)} |\n"
    content += "\n"

    # Frameworks
    if frameworks:
        content += "## Frameworks & Tools\n\n"
        for fw in frameworks:
            content += f"- {fw}\n"
        content += "\n"

    # Table of contents
    content += "## Documentation Sections\n\n"
    for lang in lang_files:
        if lang == "python":
            content += "- [Python API Reference](api/python/index.md)\n"
        elif lang in ("javascript", "typescript"):
            content += "- [JavaScript/TypeScript API Reference](api/javascript/index.md)\n"
        else:
            content += f"- [{lang.capitalize()} API Reference](api/{lang}/index.md)\n"
    content += "- [Dependencies](dependencies/index.md)\n"

    with open(output_dir / "index.md", "w", encoding="utf-8") as f:
        f.write(content)


_CORE_DOC_TARGETS = {"dependencies", "overview", "architecture"}


def _normalize_selected_docs(selected_docs):
    """Normalize selected doc targets from CLI/API input."""
    if not selected_docs:
        return None

    if isinstance(selected_docs, str):
        raw_items = [selected_docs]
    else:
        raw_items = list(selected_docs)

    normalized = set()
    for item in raw_items:
        if item is None:
            continue
        for token in str(item).split(","):
            cleaned = token.strip().lower().replace("-", "_")
            if cleaned:
                normalized.add(cleaned)

    return normalized or None


def get_available_doc_targets(project_path, lang_filter=None):
    """Return selectable documentation target names for a project path."""
    project_path = str(Path(project_path).resolve())
    lang_files = detect_languages(project_path)

    if lang_filter:
        lang_filter = lang_filter.lower()
        lang_files = {lang_filter: lang_files.get(lang_filter, [])} if lang_filter in lang_files else {}

    targets = set(lang_files.keys()) | set(_CORE_DOC_TARGETS)
    return sorted(targets)


# ─── Main Entry Point ────────────────────────────────────────────────────────

def generate_docs(project_path, output_dir=None, lang_filter=None, verbose=True, selected_docs=None):
    """
    Main entry point: detect languages and generate all docs.

    Args:
        project_path: Path to the project root.
        output_dir: Output directory for docs (default: <project>/docs).
        lang_filter: Only generate docs for this language (optional).
        verbose: Print progress messages.
        selected_docs: Optional iterable of doc targets. Supported values include
            detected language names and core targets: dependencies, overview, architecture.

    Returns:
        Dict with generation results per language.
    """
    project_path = str(Path(project_path).resolve())
    if output_dir is None:
        output_dir = Path(project_path) / "docs"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"\n🦊 FoxNest Auto-Docs Generator (quality-first mode)")
        print(f"{'=' * 50}")
        print(f"Project: {project_path}")
        print(f"Output:  {output_dir}")
        print()

    # Detect languages
    lang_files = detect_languages(project_path)
    frameworks = detect_frameworks(project_path)

    if not lang_files:
        if verbose:
            print("⚠ No code files detected in the project.")
        return {"error": "No code files detected"}

    if verbose:
        print(f"📁 Languages detected:")
        for lang, files in sorted(lang_files.items()):
            print(f"   {lang:20s} → {len(files)} files")
        if frameworks:
            print(f"\n🔧 Frameworks: {', '.join(frameworks)}")
        print()

    # Filter if specific language requested
    if lang_filter:
        lang_filter = lang_filter.lower()
        if lang_filter in lang_files:
            lang_files = {lang_filter: lang_files[lang_filter]}
        else:
            if verbose:
                print(f"⚠ Language '{lang_filter}' not found in project.")
            return {"error": f"Language '{lang_filter}' not found"}

    selected_targets = _normalize_selected_docs(selected_docs)
    generate_dependencies = True
    generate_overview_doc = True
    generate_architecture_doc = True

    if selected_targets and "all" not in selected_targets:
        requested_langs = {lang: files for lang, files in lang_files.items() if lang.lower() in selected_targets}
        generate_dependencies = "dependencies" in selected_targets
        generate_overview_doc = "overview" in selected_targets
        generate_architecture_doc = "architecture" in selected_targets

        if not requested_langs and not (generate_dependencies or generate_overview_doc or generate_architecture_doc):
            available = sorted(set(lang_files.keys()) | _CORE_DOC_TARGETS)
            return {
                "error": "No valid selected docs found.",
                "available_docs": available,
            }

        lang_files = requested_langs

    results = {}
    all_module_summaries = []  # collected across all languages for architecture doc

    # Generate docs per language
    for lang, files in sorted(lang_files.items()):
        if verbose:
            print(f"📝 Generating {lang} docs ({len(files)} files)...")

        if lang == "python":
            results["python"] = generate_python_docs(project_path, files, output_dir)
            all_module_summaries.extend(results["python"].get("module_summaries", []))
        elif lang in ("javascript", "typescript"):
            js_files = lang_files.get("javascript", [])
            ts_files = lang_files.get("typescript", [])
            if "javascript" not in results and "typescript" not in results:
                results["javascript"] = generate_js_ts_docs(project_path, js_files, ts_files, output_dir)
                all_module_summaries.extend(results["javascript"].get("module_summaries", []))
        elif lang == "xml":
            results["xml"] = generate_xml_docs(project_path, files, output_dir)
        else:
            results[lang] = generate_generic_docs(project_path, lang, files, output_dir)
            all_module_summaries.extend(results[lang].get("module_summaries", []))

    # Generate dependency docs
    if generate_dependencies:
        if verbose:
            print(f"📦 Generating dependency docs...")
        generate_dependency_docs(project_path, frameworks, output_dir)

    # Generate project overview
    if generate_overview_doc:
        if verbose:
            print(f"📄 Generating project overview...")
        generate_overview(project_path, lang_files, frameworks, output_dir)

    # ── Architecture overview (LLM, runs after all per-file docs) ────────────
    llm = get_llm_helper()
    if llm and all_module_summaries and generate_architecture_doc:
        if verbose:
            print(f"🏗️  Generating architecture overview ({len(all_module_summaries)} modules)…")
        try:
            project_name = Path(project_path).name
            arch_text = llm.generate_architecture_overview(
                module_summaries=all_module_summaries,
                project_name=project_name,
                languages=list(lang_files.keys()),
                frameworks=frameworks,
            )
            if arch_text:
                arch_file = output_dir / "architecture.md"
                with open(arch_file, "w", encoding="utf-8") as _af:
                    _af.write(f"# {project_name} — Architecture Overview\n\n")
                    _af.write(f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} by FoxNest Auto-Docs*\n\n")
                    _af.write(arch_text)
                if verbose:
                    print(f"   ✅ Architecture overview written to {arch_file}")
        except Exception as _ae:
            if verbose:
                print(f"   ⚠ Architecture overview generation failed: {_ae}")

    # Summary
    if verbose:
        print(f"\n{'=' * 50}")
        print(f"✅ Documentation generated successfully!")
        print(f"   Output: {output_dir}")
        print()
        for lang, stats in results.items():
            cov = stats.get("coverage", 0)
            files_count = stats.get("files", 0)
            print(f"   {lang:20s} → {files_count} files, {cov}% documented")
        print()

    # Write results metadata
    # Strip module_summaries from serialised results (they are large and internal)
    clean_results = {}
    for k, v in results.items():
        clean_results[k] = {key: val for key, val in v.items() if key != "module_summaries"}

    meta = {
        "generated_at": datetime.now().isoformat(),
        "project_path": project_path,
        "languages": clean_results,
        "frameworks": frameworks,
        "selected_docs": sorted(selected_targets) if selected_targets else ["all"],
    }
    with open(output_dir / "docs_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    return results


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    lang = sys.argv[2] if len(sys.argv) > 2 else None
    generate_docs(path, lang_filter=lang)
