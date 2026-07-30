"""
FoxNest AutoDocs v2.

Template-driven, bounded-retry docs generation with doc-specific context slicing.
Designed to improve document specificity without over-constraining the model.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


IGNORE_DIRS = {
    ".git", ".fox", "node_modules", "venv", ".venv", "__pycache__", "build", "dist",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode", "docs", "_archived_unused_20260213_043002",
}

DOC_NAME_TO_TEMPLATE = {
    "README.md": "TEMPLATE_README.md",
    "API Documentation": "TEMPLATE_API.md",
    "Architecture": "TEMPLATE_ARCHITECTURE.md",
    "Installation Guide": "TEMPLATE_INSTALLATION.md",
    "Database Schema": "TEMPLATE_DATABASE.md",
    "Requirements (SRS)": "TEMPLATE_REQUIREMENTS.md",
    "Use Cases": "TEMPLATE_USE_CASES.md",
    "Release Notes": "TEMPLATE_RELEASE_NOTES.md",
    "User Manual": "TEMPLATE_USER_MANUAL.md",
    "API Usage Guide": "TEMPLATE_API_USAGE.md",
    "Database ER": "TEMPLATE_DATABASE_ER.md",
    "index.md": "TEMPLATE_INDEX.md",
}

DOC_NAME_TO_FILENAME = {
    "README.md": "README.md",
    "API Documentation": "API.md",
    "Architecture": "ARCHITECTURE.md",
    "Installation Guide": "INSTALLATION.md",
    "Database Schema": "DATABASE.md",
    "Requirements (SRS)": "REQUIREMENTS.md",
    "Use Cases": "USE_CASES.md",
    "Release Notes": "RELEASE_NOTES.md",
    "User Manual": "USER_MANUAL.md",
    "API Usage Guide": "API_USAGE.md",
    "Database ER": "DATABASE_ER.md",
    "index.md": "index.md",
}


@dataclass
class DocResult:
    name: str
    path: Path
    content: str


class QwenClient:
    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url
        self.api_url = f"{base_url}/api/generate"
        self.tags_url = f"{base_url}/api/tags"
        self.timeout = int(os.getenv("FOXNEST_LLM_REQUEST_TIMEOUT_SECONDS", "240"))
        self.call_count = 0

    def validate(self) -> None:
        resp = requests.get(self.tags_url, timeout=15)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        available = {m.get("name") for m in payload.get("models", []) if isinstance(m, dict)}
        if self.model not in available:
            raise RuntimeError(f"LLM model '{self.model}' not available in Ollama")

    def generate(self, prompt: str, max_tokens: int = 5500, temperature: float = 0.1) -> str:
        self.call_count += 1
        resp = requests.post(
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
                },
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()


class AutoDocsV2Service:
    def __init__(self, templates_dir: Path):
        self.templates_dir = templates_dir

    def _load_template(self, doc_name: str) -> str:
        fname = DOC_NAME_TO_TEMPLATE.get(doc_name, "")
        if not fname:
            return ""
        path = self.templates_dir / fname
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    def _load_index_template(self) -> str:
        path = self.templates_dir / DOC_NAME_TO_TEMPLATE.get("index.md", "TEMPLATE_INDEX.md")
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    def _render_index_from_template(
        self,
        template: str,
        project_name: str,
        llm_model: str,
        project_info: Dict[str, Any],
        generated_files: List[str],
        skipped_docs: List[Dict[str, str]],
    ) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Extract some high-level signals (best-effort).
        languages = project_info.get("languages") or []
        primary_lang = (languages[0][0] if languages else "Not detected from repository evidence")
        api_count = len(project_info.get("api_endpoints") or [])
        file_count = int(project_info.get("file_count") or 0)
        line_count = int(project_info.get("line_count") or 0)
        # In v2, frameworks are not deeply inferred; keep deterministic.
        framework = "Not detected from repository evidence"
        model_count = 0  # v2 doesn't extract ORM models today

        # Document generation status table values.
        skipped_by_name = {s.get("name"): s.get("reason", "") for s in skipped_docs or []}
        def _status(name: str) -> tuple[str, str]:
            if name in skipped_by_name:
                return "⏭️ Skipped", skipped_by_name.get(name, "")
            return "✅ Generated", ""

        # Files list: prefer README.md + docx files when present (post-processing may convert later).
        gen_names = {Path(p).name for p in generated_files or []}

        # Fill placeholders in the template (string replace keeps section headings intact).
        out = template
        out = out.replace("[Project Name]", project_name or "Unknown project")
        out = out.replace("YYYY-MM-DD HH:MM", now)
        out = out.replace("[LLM model used]", llm_model or "unknown")
        out = out.replace("[project name]", project_name or "Unknown project")
        out = out.replace("[language]", str(primary_lang))
        out = out.replace("[framework]", framework)
        out = out.replace("[N] detected", str(api_count) + " detected", 1)
        out = out.replace("[N] detected", str(model_count) + " detected", 1)
        out = out.replace("[N] files, [N] lines", f"{file_count} files, {line_count:,} lines")
        out = out.replace("[model name]", llm_model or "unknown")

        # Patch the Document Generation Status rows with real statuses.
        # Keep headings/structure, only replace the status tokens.
        replacements = {
            "README.md": _status("README.md"),
            "API Documentation": _status("API Documentation"),
            "Architecture": _status("Architecture"),
            "Installation Guide": _status("Installation Guide"),
            "Database Schema": _status("Database Schema"),
            "Requirements (SRS)": _status("Requirements (SRS)"),
            "Use Cases": _status("Use Cases"),
            "Release Notes": _status("Release Notes"),
            "User Manual": _status("User Manual"),
            "API Usage Guide": _status("API Usage Guide"),
            "Database ER": _status("Database ER"),
        }
        lines = out.splitlines()
        fixed = []
        for line in lines:
            if line.strip().startswith("|") and "|" in line:
                for doc, (status, note) in replacements.items():
                    # match "| <doc> | <status> | <notes> |"
                    if line.lower().startswith(f"| {doc.lower()} "):
                        parts = [p.strip() for p in line.strip().strip("|").split("|")]
                        if len(parts) >= 3:
                            parts[1] = status
                            if note:
                                parts[2] = (note[:120] + "…") if len(note) > 121 else note
                            line = "| " + " | ".join(parts) + " |"
                        break
            fixed.append(line)
        out = "\n".join(fixed).strip() + "\n"
        # If README is not in generated set, keep the link but it's fine; template requires it.
        return out

    def _extract_required_headings(self, template: str) -> List[str]:
        return [line.strip() for line in template.splitlines() if line.startswith("## ")]

    def _safe_read_text(self, path: Path, max_chars: int = 30000) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        except Exception:
            return ""

    def _collect_context(self, project_path: Path) -> Dict[str, Any]:
        languages: Dict[str, int] = {}
        tree: List[str] = []
        key_files: Dict[str, str] = {}
        endpoints: List[str] = []
        module_links: List[str] = []
        imports_by_file: Dict[str, List[str]] = {}
        subprojects: List[Dict[str, Any]] = []
        file_count = 0
        line_count = 0

        ext_lang = {
            ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
            ".tsx": "typescript", ".java": "java", ".go": "go", ".rs": "rust", ".php": "php",
            ".rb": "ruby", ".c": "c", ".cpp": "cpp", ".h": "c", ".sql": "sql", ".sh": "shell",
        }
        key_file_names = {
            "README.md", "requirements.txt", "pyproject.toml", "package.json",
            "go.mod", "Cargo.toml", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        }
        # Heuristics for "mixed monorepo" module detection.
        module_markers = {
            "package.json": "node",
            "requirements.txt": "python",
            "pyproject.toml": "python",
            "Cargo.toml": "rust",
            "go.mod": "go",
            "build.gradle": "gradle",
            "settings.gradle": "gradle",
            "pom.xml": "maven",
            "Dockerfile": "docker",
            "docker-compose.yml": "docker-compose",
            "docker-compose.yaml": "docker-compose",
        }
        seen_module_roots: set[str] = set()

        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            for name in files:
                path = Path(root) / name
                rel = str(path.relative_to(project_path)).replace("\\", "/")
                if any(part in IGNORE_DIRS for part in Path(rel).parts):
                    continue
                file_count += 1
                if len(tree) < 500:
                    tree.append(rel)

                ext = path.suffix.lower()
                if ext in ext_lang:
                    languages[ext_lang[ext]] = languages.get(ext_lang[ext], 0) + 1
                    content = self._safe_read_text(path)
                    line_count += len(content.splitlines())
                    if ext == ".py":
                        for m in re.finditer(r"@(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]", content):
                            endpoints.append(f"{m.group(1).upper()} {m.group(2)} ({rel})")
                        mods = []
                        for m in re.finditer(
                            r"^\s*(?:from\s+([a-zA-Z_][\w\.]*)\s+import|import\s+([a-zA-Z_][\w\.]*))",
                            content,
                            flags=re.MULTILINE,
                        ):
                            mod = (m.group(1) or m.group(2) or "").split(".")[0]
                            if mod and mod not in mods:
                                mods.append(mod)
                        imports_by_file[rel] = mods[:20]

                if name in key_file_names and name not in key_files:
                    key_files[name] = self._safe_read_text(path, max_chars=5000)

                # Subproject markers: keep a small list of module roots + build system hints.
                if name in module_markers:
                    root_rel = str(Path(root).relative_to(project_path)).replace("\\", "/") or "."
                    if root_rel not in seen_module_roots:
                        seen_module_roots.add(root_rel)
                        subprojects.append(
                            {
                                "path": root_rel,
                                "marker": name,
                                "type_hint": module_markers[name],
                            }
                        )

        internal = {Path(p).stem for p in imports_by_file}
        for rel, mods in imports_by_file.items():
            linked = [m for m in mods if m in internal]
            if linked:
                module_links.append(f"{rel} -> {', '.join(linked[:8])}")

        return {
            "project_name": project_path.name,
            "file_count": file_count,
            "line_count": line_count,
            "languages": sorted(languages.items(), key=lambda x: x[0]),
            "tree": tree[:400],
            "key_files": key_files,
            "api_endpoints": endpoints[:160],
            "module_relationships": module_links[:220],
            "subprojects": subprojects[:60],
        }

    def _doc_profile(self, doc_name: str, template: str) -> Dict[str, Any]:
        headings = self._extract_required_headings(template)
        lowered = template.lower()
        keywords = [
            "requirements", "functional", "non-functional", "acceptance",
            "architecture", "component", "data flow", "deployment",
            "api", "endpoint", "request", "response", "auth",
            "database", "entity", "relationship", "schema", "index",
            "installation", "prerequisites", "configuration", "troubleshooting",
            "use case", "actor", "scenario", "preconditions", "postconditions",
            "release", "changelog", "upgrade", "breaking changes",
        ]
        return {
            "purpose": f"Generate {doc_name} using its template structure.",
            "must_focus": [re.sub(r"^##\s+", "", h) for h in headings] or ["Overview", "Details"],
            "keywords": [k for k in keywords if k in lowered][:24],
        }

    def _slice_context_for_doc(self, context: Dict[str, Any], doc_name: str) -> Dict[str, Any]:
        base = {
            "project_name": context.get("project_name"),
            "file_count": context.get("file_count"),
            "line_count": context.get("line_count"),
            "languages": context.get("languages", []),
            "subprojects": context.get("subprojects", [])[:30],
        }
        tree = context.get("tree", [])
        key_files = context.get("key_files", {})
        endpoints = context.get("api_endpoints", [])
        links = context.get("module_relationships", [])

        if doc_name in {"API Documentation", "API Usage Guide"}:
            base["api_endpoints"] = endpoints[:200]
            base["module_relationships"] = links[:50]
            base["tree"] = [p for p in tree if "/api/" in p or "route" in p.lower() or p.endswith(".py")][:140]
            base["key_files"] = {k: v for k, v in key_files.items() if k in {"README.md", "requirements.txt", "pyproject.toml"}}
        elif doc_name in {"Database Schema", "Database ER"}:
            base["api_endpoints"] = endpoints[:40]
            base["module_relationships"] = [m for m in links if any(x in m.lower() for x in ["model", "database", "schema", "crud"])][:160]
            base["tree"] = [p for p in tree if any(x in p.lower() for x in ["database", "model", "schema", "migration", ".sql"])][:180]
            base["key_files"] = {k: v for k, v in key_files.items() if k in {"README.md", "requirements.txt", "pyproject.toml"}}
        elif doc_name == "Installation Guide":
            base["api_endpoints"] = endpoints[:40]
            base["module_relationships"] = links[:30]
            base["tree"] = [p for p in tree if any(x in p.lower() for x in ["docker", "service", "requirement", "config", ".env", "setup", "install"])][:180]
            base["key_files"] = {k: v for k, v in key_files.items() if k in {
                "README.md", "requirements.txt", "pyproject.toml", "package.json",
                "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
            }}
        elif doc_name == "Architecture":
            base["api_endpoints"] = endpoints[:100]
            base["module_relationships"] = links[:260]
            base["tree"] = tree[:260]
            base["key_files"] = key_files
        else:
            base["api_endpoints"] = endpoints[:120]
            base["module_relationships"] = links[:100]
            base["tree"] = tree[:180]
            base["key_files"] = key_files
        return base

    def _normalize(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower()).strip()

    def _similarity(self, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, self._normalize(a)[:10000], self._normalize(b)[:10000]).ratio()

    def _summary(self, content: str) -> str:
        lines = [l.strip() for l in (content or "").splitlines() if l.strip()]
        return " ".join(lines[:8])[:320]

    def _render_prompt(
        self,
        doc_name: str,
        template: str,
        headings: List[str],
        context: Dict[str, Any],
        profile: Dict[str, Any],
        prior_doc_summaries: List[Dict[str, str]],
    ) -> str:
        prior_block = "\n".join(f"- {i.get('name')}: {i.get('summary')}" for i in prior_doc_summaries[-6:]) or "None"
        return f"""You are a senior staff software engineer and technical writer.
Generate ONE professional documentation artifact.

Document target: {doc_name}
Project: {context.get("project_name")}
Document intent: {profile.get("purpose")}

Hard rules:
1) Output valid Markdown only.
2) Follow template structure exactly (headings/order).
3) Preserve EVERY template section heading in the SAME order. Do NOT remove sections.
   If a section has no evidence, write "Not detected from repository evidence" or "Not yet applicable" and explain briefly.
4) Use only repository evidence; if unknown write "Not detected from repository evidence".
5) Do not leave placeholders like [Project Name], TODO, TBD, or YYYY-MM-DD.
5) Keep content specific to this document purpose.
6) Avoid copying paragraphs from prior generated docs.

Must-focus sections/signals:
- {", ".join(profile.get("must_focus", []))}
Template signal keywords:
- {", ".join(profile.get("keywords", []))}

Required headings:
{json.dumps(headings)}

Prior doc summaries (avoid overlap):
{prior_block}

Repository context:
{json.dumps(context, indent=2)}

Template blueprint:
{template}

Return only final Markdown content.
"""

    def _validate_doc(
        self,
        content: str,
        required_headings: List[str],
        prior_contents: List[Tuple[str, str]],
    ) -> Tuple[bool, str, Dict[str, Any]]:
        text = (content or "").strip()
        if not text or len(text) < 180:
            return False, "empty or too short", {}
        if re.search(r"\[placeholder\]|\[(Project Name|Your Name)\]|TODO|TBD|YYYY-MM-DD", text, flags=re.IGNORECASE):
            return False, "contains unresolved placeholders", {}
        # ── Snippet size enforcement ──────────────────────────────────────
        code_blocks = re.findall(r"```[\s\S]*?```", text)
        oversized = [b for b in code_blocks if len(b.splitlines()) > 22]  # 20 lines + 2 fence lines
        if oversized:
            largest = max(len(b.splitlines()) for b in oversized) - 2
            return (
                False,
                (
                    f"contains {len(oversized)} oversized code block(s) "
                    f"(max {largest} lines). Snippets must be ≤20 lines."
                ),
                {"oversized_blocks": len(oversized)},
            )
        # ── End snippet size enforcement ──────────────────────────────────

        missing = [h for h in required_headings if h not in text]
        if missing:
            return False, f"missing required heading: {missing[0]}", {"missing_headings": missing}
        # Order check: required headings must appear in the same sequence as the template.
        last_idx = -1
        for h in required_headings:
            idx = text.find(h)
            if idx < last_idx:
                return False, "required headings appear out of order", {"headings_out_of_order": True}
            last_idx = idx

        max_sim = 0.0
        nearest = ""
        for prev_name, prev in prior_contents:
            sim = self._similarity(text, prev)
            if sim > max_sim:
                max_sim = sim
                nearest = prev_name
        if max_sim >= 0.90:
            return False, f"nearly duplicate of {nearest} (similarity={max_sim:.2f})", {"max_similarity": max_sim}

        quality = {
            "max_similarity": round(max_sim, 3),
            "nearest_doc": nearest,
            "soft_similarity_warning": max_sim >= 0.72,
        }
        return True, "", quality

    def _repair_doc(
        self,
        llm: QwenClient,
        doc_name: str,
        template: str,
        required_headings: List[str],
        context: Dict[str, Any],
        broken_doc: str,
        reason: str,
        profile: Dict[str, Any],
        prior_doc_summaries: List[Dict[str, str]],
    ) -> str:
        prior_block = "\n".join(f"- {i.get('name')}: {i.get('summary')}" for i in prior_doc_summaries[-6:]) or "None"
        prompt = f"""Repair this Markdown document.
Target: {doc_name}
Reason: {reason}
Intent: {profile.get("purpose")}
Focus: {", ".join(profile.get("must_focus", []))}
Keywords: {", ".join(profile.get("keywords", []))}

Required headings:
{json.dumps(required_headings)}

Prior doc summaries:
{prior_block}

Context:
{json.dumps(context, indent=2)}

Template:
{template}

Current draft:
{broken_doc[:24000]}

Return corrected Markdown only.
"""
        return llm.generate(prompt, max_tokens=5500, temperature=0.07)

    def generate(
        self,
        project_path: Path,
        output_dir: Path,
        llm_model: str,
        selected_docs: Optional[List[str]] = None,
        project_name_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        llm = QwenClient(llm_model)
        llm.validate()

        context = self._collect_context(project_path)
        if project_name_override:
            context["project_name"] = project_name_override

        selected = set(selected_docs) if selected_docs else set(DOC_NAME_TO_TEMPLATE.keys())
        if "index.md" not in selected:
            selected.add("index.md")

        generated: List[str] = []
        skipped: List[Dict[str, str]] = []
        results: List[DocResult] = []
        prior_contents: List[Tuple[str, str]] = []
        prior_summaries: List[Dict[str, str]] = []
        quality_notes: List[Dict[str, Any]] = []

        for doc_name in DOC_NAME_TO_TEMPLATE.keys():
            if doc_name not in selected:
                skipped.append({"name": doc_name, "reason": "Not selected by user"})
                continue

            template = self._load_template(doc_name)
            if not template:
                skipped.append({"name": doc_name, "reason": f"Missing template: {DOC_NAME_TO_TEMPLATE.get(doc_name)}"})
                continue

            headings = self._extract_required_headings(template)
            profile = self._doc_profile(doc_name, template)
            doc_ctx = self._slice_context_for_doc(context, doc_name)
            prompt = self._render_prompt(doc_name, template, headings, doc_ctx, profile, prior_summaries)

            content = ""
            validated = False
            last_reason = "unknown"
            last_quality: Dict[str, Any] = {}
            for attempt in range(3):
                if attempt == 0:
                    content = llm.generate(prompt, max_tokens=6500, temperature=0.1)
                else:
                    content = self._repair_doc(
                        llm=llm,
                        doc_name=doc_name,
                        template=template,
                        required_headings=headings,
                        context=doc_ctx,
                        broken_doc=content,
                        reason=last_reason,
                        profile=profile,
                        prior_doc_summaries=prior_summaries,
                    )
                ok, reason, quality = self._validate_doc(content, headings, prior_contents)
                last_reason = reason
                last_quality = quality or {}
                if ok:
                    validated = True
                    break

            if not validated:
                skipped.append({"name": doc_name, "reason": f"Validation failed after retries: {last_reason}"})
                continue

            out = output_dir / DOC_NAME_TO_FILENAME[doc_name]
            out.write_text(content.strip() + "\n", encoding="utf-8")
            generated.append(str(out))
            results.append(DocResult(name=doc_name, path=out, content=content))
            prior_contents.append((doc_name, content))
            prior_summaries.append({"name": doc_name, "summary": self._summary(content)})
            if last_quality:
                quality_notes.append({"name": doc_name, **last_quality})

        # deterministic index refresh
        idx = output_dir / "index.md"
        if idx.exists():
            tmpl = self._load_index_template()
            if tmpl:
                rendered = self._render_index_from_template(
                    template=tmpl,
                    project_name=context.get("project_name") or "",
                    llm_model=llm.model,
                    project_info=context,
                    generated_files=generated,
                    skipped_docs=skipped,
                )
                idx.write_text(rendered, encoding="utf-8")
                # Also write INDEX.md to satisfy template contract wording while keeping index.md for UI.
                (output_dir / "INDEX.md").write_text(rendered, encoding="utf-8")
            else:
                # Fallback: minimal index
                links = [f"- [{r.name}]({r.path.name})" for r in sorted(results, key=lambda x: x.path.name) if r.path.name != "index.md"]
                rendered = "\n".join(
                    [
                        f"# Project Documentation: {context.get('project_name')}",
                        "",
                        f"*Auto-generated by FoxNest AutoDocs v2 on {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
                        "",
                        "## Documents",
                        "",
                        *links,
                        "",
                    ]
                ).strip() + "\n"
                idx.write_text(rendered, encoding="utf-8")
                (output_dir / "INDEX.md").write_text(rendered, encoding="utf-8")

        return {
            "generated_files": generated,
            "skipped_docs": skipped,
            "project_info": {
                "name": context.get("project_name"),
                "languages": [name for name, _ in context.get("languages", [])],
                "frameworks": [],
                "file_count": context.get("file_count", 0),
                "line_count": context.get("line_count", 0),
            },
            "stats": {
                "llm_model": llm.model,
                "llm_calls": llm.call_count,
                "selected_docs": sorted(selected),
                "pipeline": "autodocs_v2",
                "quality_notes": quality_notes,
            },
        }
