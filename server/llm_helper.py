"""
LLM Helper for intelligent code documentation generation.
Uses Ollama with Qwen models for high-quality code explanations.

Quality-first design — every prompt is written to maximise accuracy and completeness.
Processing time is not a constraint; documentation quality is.
"""

import requests
import json
import os
import time
from typing import Optional, Dict, Any, List


def _extract_json(text: str) -> Optional[str]:
    """Strip markdown fences from a model response and return the raw JSON string."""
    text = text.strip()
    if "```json" in text:
        s = text.find("```json") + 7
        e = text.rfind("```")
        return text[s:e].strip()
    if "```" in text:
        s = text.find("```") + 3
        e = text.rfind("```")
        return text[s:e].strip()
    # Try to find first { and last } in case model output extra prose
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        return text[first:last + 1]
    return text


class LLMHelper:
    """Helper class for LLM-powered documentation generation.
    
    Quality-first: all generation calls use large token budgets and
    include full file context so the model understands each symbol in
    its real setting rather than as an isolated snippet.
    """

    def __init__(self, model: str = None, base_url: str = "http://localhost:11434"):
        self.model = model or os.getenv("FOXNEST_DOCS_MODEL", "qwen2.5-coder:7b")
        self.base_url = base_url
        self.api_url = f"{base_url}/api/generate"

    def is_available(self) -> bool:
        """Check if Ollama service is running."""
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def _call(self, prompt: str, num_predict: int = 2000, temperature: float = 0.15,
              timeout: int = 240, retries: int = 2) -> Optional[str]:
        """
        Make a single Ollama generate call.  Retries on network/timeout errors.
        Returns the raw response string, or None on failure.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": 0.92,
                "num_predict": num_predict,
                "num_ctx": 65536,
                "repeat_penalty": 1.05,
            },
        }
        for attempt in range(retries + 1):
            try:
                response = requests.post(self.api_url, json=payload, timeout=timeout)
                if response.status_code == 200:
                    return response.json().get("response", "").strip()
            except requests.exceptions.Timeout:
                if attempt < retries:
                    print(f"  ⚠ LLM timeout (attempt {attempt + 1}/{retries + 1}), retrying…")
                    time.sleep(2)
            except Exception as e:
                print(f"  ⚠ LLM call error: {e}")
                break
        return None

    def _parse_json_response(self, text: str) -> Optional[Dict]:
        """Parse JSON from model output, with graceful fallback."""
        if not text:
            return None
        cleaned = _extract_json(text)
        if not cleaned:
            return None
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Last-ditch: replace common model quirks and retry
            try:
                fixed = cleaned.replace("\n", " ").replace("\r", "")
                return json.loads(fixed)
            except Exception:
                return None

    # ─── Module / file level ────────────────────────────────────────────────

    def generate_module_overview(
        self,
        file_content: str,
        language: str,
        filename: str,
    ) -> Optional[str]:
        """
        Read an ENTIRE source file and produce a holistic Markdown description
        of what the module does, its architecture, and how it fits in the project.
        
        Returns a Markdown string (not JSON) suitable for insertion directly into
        the documentation as a module-level overview section.
        """
        # Send the full file so the model reads all logic before writing docs.
        # Hard cap at 120 000 chars to avoid hitting protocol/memory limits.
        body = file_content[:120000]
        if len(file_content) > 120000:
            body += f"\n\n# ... ({len(file_content) - 120000} more chars — file exceeds cap) ..."

        prompt = f"""You are a senior {language} developer writing detailed technical documentation for a professional developer audience.

Read the ENTIRE {language} source file below in full before writing anything.

File: {filename}

```{language}
{body}
```

Write a comprehensive Markdown module overview with the following sections (use ## for headings):

## Purpose
A clear 3–5 sentence explanation of what this module/file is responsible for, what problem it solves, and how it is used by the rest of the project.

## Key Responsibilities
A bullet list of 4–8 specific responsibilities or capabilities this file provides.

## Architecture Notes
Describe the design patterns, notable abstractions, important data flows, or architectural decisions visible in the code (2–4 sentences). If the file is a single-purpose utility, describe its approach instead.

## Public API Summary
List every public function, class, or exported symbol with a one-line description of what it does. Format as:
- `symbol_name(...)` — what it does

## Dependencies & Integration
List external packages imported and describe what they are used for, plus how this file connects to the rest of the codebase.

Be thorough and precise. Base everything strictly on the code above — do not invent behaviour."""

        result = self._call(prompt, num_predict=3000, temperature=0.15, timeout=600)
        return result if result else None

    def generate_architecture_overview(
        self,
        module_summaries: List[Dict[str, str]],
        project_name: str,
        languages: List[str],
        frameworks: List[str],
    ) -> Optional[str]:
        """
        Given summaries of all modules, produce a project-wide architecture document.
        
        Args:
            module_summaries: List of {"path": str, "summary": str} dicts.
            project_name:     Name of the project.
            languages:        Detected languages.
            frameworks:       Detected frameworks/tools.
        
        Returns a Markdown string.
        """
        summaries_text = ""
        for ms in module_summaries[:50]:  # cap to avoid exceeding context
            summaries_text += f"\n\n### {ms['path']}\n{ms['summary'][:800]}"

        lang_list = ", ".join(languages)
        fw_list = ", ".join(frameworks) if frameworks else "none detected"

        prompt = f"""You are a senior software architect writing a comprehensive architecture overview for a project called "{project_name}".

Languages used: {lang_list}
Frameworks/tools: {fw_list}

Below are summaries of every module in the project:
{summaries_text}

Write a detailed Markdown architecture overview with the following sections (## headings):

## System Architecture Overview
High-level description of the system design, major components, and how they interact (4–8 sentences).

## Component Map
A bullet list of every major component/module, what layer it belongs to (backend, frontend, client, database, etc.), and its core responsibility.

## Data Flow
Describe the primary data flows through the system (e.g. "client → server → database → response"). Use numbered steps.

## Technology Stack
Table with columns: Layer | Technology | Purpose.

## Entry Points
List all entry points (CLI tools, web servers, scripts, etc.) with usage instructions.

## Key Design Decisions
4–6 bullet points describing notable design patterns or architectural choices visible in the codebase.

## Developer Quickstart
A step-by-step guide (numbered list) to get the project running locally, based strictly on what the code reveals.

Be precise and thorough. Only describe what is actually present in the code summaries above."""

        result = self._call(prompt, num_predict=4000, temperature=0.15, timeout=600)
        return result if result else None

    # ─── Function / method level ─────────────────────────────────────────────

    def generate_function_docs(
        self,
        code: str,
        language: str = "python",
        file_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Generate intelligent documentation for a function/method.
        
        Args:
            code:         Full source code of the function/method.
            language:     Programming language.
            file_context: Brief description of the containing file/module
                          (e.g. imports summary, module purpose) so the LLM
                          understands what the function operates on.
        
        Returns dict with keys: description, parameters, returns, notes.
        """
        ctx_block = ""
        if file_context:
            ctx_block = f"""File context (module this function belongs to):
{file_context}

"""
        prompt = f"""You are an expert {language} developer writing precise, complete technical documentation for a professional audience.

{ctx_block}Read the following {language} function COMPLETELY and document it accurately. Do not guess — base every field strictly on the actual code.

```{language}
{code}
```

Respond with ONLY a valid JSON object — no markdown fences, no commentary, no prose outside the JSON. Use these exact fields:
{{
  "description": "3–5 sentence technical description: what the function does, how it does it (describe the algorithm or logic path), and when it should be called. Be specific — name the data structures it reads/writes.",
  "parameters": {{
    "param_name": "type and full purpose — e.g. 'str: the repository UUID used to look up the record in the commits table'"
  }},
  "returns": "Exact return type and a complete description of the returned value, including key dictionary fields if it returns a dict. Write 'None' if the function returns nothing.",
  "notes": "Any important edge cases, exception types raised, side-effects (DB writes, file I/O, HTTP calls), or usage warnings visible in the code. Write empty string if none."
}}"""

        text = self._call(prompt, num_predict=2000, temperature=0.15, timeout=240)
        result = self._parse_json_response(text)
        if result is None and text:
            print(f"  ⚠ LLM JSON parse failed, raw response length: {len(text)}")
        return result

    # ─── Class level ─────────────────────────────────────────────────────────

    def generate_class_docs(
        self,
        code: str,
        language: str = "python",
        file_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Generate documentation for a class.

        Args:
            code:         Full source code of the class (header + body).
            language:     Programming language.
            file_context: Module-level context string.
        
        Returns dict with keys: description, attributes, notes.
        """
        ctx_block = ""
        if file_context:
            ctx_block = f"""File context (module this class belongs to):
{file_context}

"""
        prompt = f"""You are an expert {language} developer writing precise technical documentation for a professional audience.

{ctx_block}Read the following {language} class COMPLETELY and document it accurately. Base every field strictly on the actual code.

```{language}
{code}
```

Respond with ONLY a valid JSON object — no markdown fences, no commentary. Use these exact fields:
{{
  "description": "3–5 sentence description: what this class represents, its responsibility in the system, its design pattern (e.g. service, model, utility, repository), and typical usage.",
  "attributes": {{
    "attr_name": "type and full purpose — e.g. 'dict: maps commit SHA to FileObject instances, built lazily on first access'"
  }},
  "notes": "Inheritance behaviour, thread-safety, lifecycle (init/teardown), notable design decisions, or important caveats visible in the code. Empty string if none."
}}"""

        text = self._call(prompt, num_predict=1800, temperature=0.15, timeout=240)
        result = self._parse_json_response(text)
        if result is None and text:
            print(f"  ⚠ LLM JSON parse failed for class, raw response length: {len(text)}")
        return result

    # ─── Generic symbol (any language) ───────────────────────────────────────

    def generate_symbol_docs(
        self,
        symbol_name: str,
        code_snippet: str,
        language: str,
        symbol_kind: str = "function",
        file_context: str = "",
    ) -> Optional[str]:
        """
        Generate a plain-text documentation paragraph for a symbol in any language
        (Java, Go, Rust, C, C++, Kotlin, etc.).
        
        Returns a Markdown string (not JSON) — 2–5 sentences describing the symbol.
        """
        ctx_block = ""
        if file_context:
            ctx_block = f"File context: {file_context}\n\n"

        prompt = f"""You are an expert {language} developer writing technical documentation.

{ctx_block}Analyse this {language} {symbol_kind} named `{symbol_name}` and write precise documentation.

```{language}
{code_snippet}
```

Write 3–5 sentences of precise technical documentation that explain:
1. What this {symbol_kind} does
2. Its inputs/parameters (if any) and what they represent
3. Its return value or output (if any)
4. Any important side-effects, exceptions, or usage notes

Output ONLY the documentation text. No headings, no bullet lists, no JSON, no preamble."""

        return self._call(prompt, num_predict=600, temperature=0.15, timeout=120)

    def generate_file_symbol_docs_batch(
        self,
        file_content: str,
        language: str,
        filename: str,
        symbol_names: List[str],
    ) -> Optional[Dict[str, str]]:
        """
        Document multiple symbols in one LLM call by reading the full file.
        More accurate than snippet-by-snippet because the model sees all context.
        
        Returns a dict mapping symbol_name → documentation paragraph.
        """
        symbols_list = "\n".join(f"- {s}" for s in symbol_names[:30])
        body = file_content[:14000]

        prompt = f"""You are an expert {language} developer writing comprehensive technical documentation.

Read this ENTIRE {language} file ({filename}) carefully:

```{language}
{body}
```

Now document each of the following symbols ACCURATELY based on their actual implementation:

{symbols_list}

Respond with ONLY a valid JSON object where each key is a symbol name and the value is a 3–5 sentence documentation paragraph. No markdown fences, no extra keys.

Example format:
{{
  "symbol_name": "3-5 sentence description of what this symbol does, its parameters, return value, and notable behaviour.",
  ...
}}"""

        text = self._call(prompt, num_predict=4000, temperature=0.15, timeout=600)
        result = self._parse_json_response(text)
        return result


# ─── Global singleton ────────────────────────────────────────────────────────

_llm_helper: Optional["LLMHelper"] = None


def get_llm_helper() -> Optional["LLMHelper"]:
    """Get or create the global LLMHelper singleton."""
    global _llm_helper
    if _llm_helper is None:
        _llm_helper = LLMHelper()
        if not _llm_helper.is_available():
            print("  ℹ️  LLM service not available — using basic documentation")
            _llm_helper = None
    return _llm_helper
