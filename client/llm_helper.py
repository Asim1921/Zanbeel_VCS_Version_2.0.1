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
        """Make a single Ollama generate call with retry logic."""
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
            try:
                fixed = cleaned.replace("\n", " ").replace("\r", "")
                return json.loads(fixed)
            except Exception:
                return None

    def generate_module_overview(
        self,
        file_content: str,
        language: str,
        filename: str,
    ) -> Optional[str]:
        """Read an ENTIRE source file and produce a holistic Markdown module overview."""
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
Describe the design patterns, notable abstractions, important data flows, or architectural decisions visible in the code (2–4 sentences).

## Public API Summary
List every public function, class, or exported symbol with a one-line description. Format as:
- `symbol_name(...)` — what it does

## Dependencies & Integration
List external packages imported and describe what they are used for.

Be thorough and precise. Base everything strictly on the code above."""

        result = self._call(prompt, num_predict=3000, temperature=0.15, timeout=600)
        return result if result else None

    def generate_function_docs(
        self,
        code: str,
        language: str = "python",
        file_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Generate detailed documentation for a function/method."""
        ctx_block = ""
        if file_context:
            ctx_block = f"File context (module this function belongs to):\n{file_context}\n\n"

        prompt = f"""You are an expert {language} developer writing precise, complete technical documentation for a professional audience.

{ctx_block}Read the following {language} function COMPLETELY and document it accurately. Do not guess — base every field strictly on the actual code.

```{language}
{code}
```

Respond with ONLY a valid JSON object — no markdown fences, no commentary. Use these exact fields:
{{
  "description": "3–5 sentence technical description: what the function does, how it does it, and when it should be called.",
  "parameters": {{
    "param_name": "type and full purpose"
  }},
  "returns": "Exact return type and complete description of the returned value.",
  "notes": "Edge cases, exceptions raised, side-effects, or usage warnings visible in the code. Empty string if none."
}}"""

        text = self._call(prompt, num_predict=2000, temperature=0.15, timeout=240)
        result = self._parse_json_response(text)
        if result is None and text:
            print(f"  ⚠ LLM JSON parse failed, raw response length: {len(text)}")
        return result

    def generate_class_docs(
        self,
        code: str,
        language: str = "python",
        file_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Generate detailed documentation for a class."""
        ctx_block = ""
        if file_context:
            ctx_block = f"File context (module this class belongs to):\n{file_context}\n\n"

        prompt = f"""You are an expert {language} developer writing precise technical documentation for a professional audience.

{ctx_block}Read the following {language} class COMPLETELY and document it accurately.

```{language}
{code}
```

Respond with ONLY a valid JSON object — no markdown fences, no commentary. Use these exact fields:
{{
  "description": "3–5 sentence description: what this class represents, its responsibility in the system, its design pattern, and typical usage.",
  "attributes": {{
    "attr_name": "type and full purpose"
  }},
  "notes": "Inheritance behaviour, thread-safety, lifecycle, or important caveats. Empty string if none."
}}"""

        text = self._call(prompt, num_predict=1800, temperature=0.15, timeout=240)
        result = self._parse_json_response(text)
        if result is None and text:
            print(f"  ⚠ LLM JSON parse failed for class, raw response length: {len(text)}")
        return result

    def generate_symbol_docs(
        self,
        symbol_name: str,
        code_snippet: str,
        language: str,
        symbol_kind: str = "function",
        file_context: str = "",
    ) -> Optional[str]:
        """Generate plain-text documentation for a symbol in any language."""
        ctx_block = f"File context: {file_context}\n\n" if file_context else ""

        prompt = f"""You are an expert {language} developer writing technical documentation.

{ctx_block}Analyse this {language} {symbol_kind} named `{symbol_name}` and write precise documentation.

```{language}
{code_snippet}
```

Write 3–5 sentences explaining: what it does, its parameters, its return value, and any side-effects.

Output ONLY the documentation text. No headings, no JSON, no preamble."""

        return self._call(prompt, num_predict=600, temperature=0.15, timeout=120)

    def generate_file_symbol_docs_batch(
        self,
        file_content: str,
        language: str,
        filename: str,
        symbol_names: List[str],
    ) -> Optional[Dict[str, str]]:
        """Document multiple symbols in one LLM call by reading the full file."""
        symbols_list = "\n".join(f"- {s}" for s in symbol_names[:30])
        body = file_content[:14000]

        prompt = f"""You are an expert {language} developer writing comprehensive technical documentation.

Read this ENTIRE {language} file ({filename}) carefully:

```{language}
{body}
```

Now document each of the following symbols ACCURATELY based on their actual implementation:

{symbols_list}

Respond with ONLY a valid JSON object where each key is a symbol name and the value is a 3–5 sentence documentation paragraph. No markdown fences, no extra keys."""

        text = self._call(prompt, num_predict=4000, temperature=0.15, timeout=600)
        return self._parse_json_response(text)


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
