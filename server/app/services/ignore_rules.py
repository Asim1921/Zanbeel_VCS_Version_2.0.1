"""`.foxignore` pattern matching.

Implements the familiar gitignore syntax so a repository can declare which paths must
never enter the object store:

    # comments and blank lines are skipped
    build/                  trailing slash: directory contents only
    *.zip                   glob, matched at any depth
    /secrets.env            leading slash: anchored to the repository root
    docs/**/*.tmp           ** spans directory separators
    !keep/important.zip     leading ! re-includes a path an earlier rule excluded

Rules are evaluated in order and the last one that matches decides, so a negation only
works if it comes after the rule that excluded the path.

The same syntax is implemented client-side in ``client/fox.py``; keep the two in step.
"""

import re
from typing import Iterable, List, Optional, Tuple

#: Applied when a repository has no ``.foxignore`` of its own. Deliberately conservative:
#: version control metadata, dependency trees, build output, and the archive/installer
#: formats that have no business in a source repository.
DEFAULT_IGNORE_PATTERNS: Tuple[str, ...] = (
    # version control + tooling metadata
    ".fox/", ".git/", ".svn/", ".hg/",
    # dependency trees and virtualenvs
    "node_modules/", "venv/", ".venv/", "env/", "vendor/",
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".tox/", ".mypy_cache/",
    # build output
    "build/", "dist/", "out/", "target/", "bin/", "obj/",
    "*.o", "*.so", "*.dylib", "*.class",
    # archives and installers
    "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.rar", "*.7z",
    "*.exe", "*.msi", "*.dmg", "*.apk", "*.iso", "*.bacpac",
    # local noise
    ".DS_Store", "Thumbs.db", "*.log", "*.tmp", "*.swp",
)

IGNORE_FILENAME = ".foxignore"


def _translate(pattern: str) -> str:
    """Translate one gitignore glob into a regular expression source string."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                # `**/` spans zero or more directories; a bare `**` spans anything.
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


class _Rule:
    __slots__ = ("regex", "negated", "dir_only", "source")

    def __init__(self, regex: "re.Pattern[str]", negated: bool, dir_only: bool, source: str):
        self.regex = regex
        self.negated = negated
        self.dir_only = dir_only
        self.source = source


def _compile_rule(raw: str) -> Optional[_Rule]:
    line = raw.rstrip("\n").rstrip("\r")
    # A trailing backslash escapes trailing whitespace; otherwise strip it.
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

    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    if not line:
        return None

    anchored = line.startswith("/") or "/" in line.rstrip("/")
    line = line.lstrip("/")
    if not line:
        return None

    body = _translate(line)
    # An anchored pattern is matched from the repository root; an unanchored one
    # (no slash in it) matches the same way at any depth, like gitignore's basename rule.
    prefix = "" if anchored else "(?:.*/)?"
    # Matching a directory also matches everything beneath it.
    regex = re.compile("^" + prefix + body + "(?:/.*)?$")
    return _Rule(regex, negated, dir_only, raw.strip())


class IgnoreMatcher:
    """Ordered `.foxignore` rules; the last matching rule wins."""

    def __init__(self, rules: List[_Rule], patterns: List[str]):
        self._rules = rules
        self.patterns = patterns

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "IgnoreMatcher":
        rules, patterns = [], []
        for raw in lines:
            rule = _compile_rule(raw)
            if rule is not None:
                rules.append(rule)
                patterns.append(rule.source)
        return cls(rules, patterns)

    @classmethod
    def from_text(cls, text: Optional[str]) -> "IgnoreMatcher":
        return cls.from_lines((text or "").splitlines())

    @classmethod
    def default(cls) -> "IgnoreMatcher":
        return cls.from_lines(DEFAULT_IGNORE_PATTERNS)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def matched_rule(self, path: str) -> Optional[str]:
        """Return the pattern that excludes `path`, or None when it is allowed."""
        # Strip a leading "./" prefix and any leading slashes -- but by prefix, never with
        # lstrip(char_set), which would also eat the dot of a dotfile like ".git/config".
        normalized = (path or "").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        if not normalized:
            return None

        decision: Optional[_Rule] = None
        for rule in self._rules:
            # A directory-only rule can still exclude a file that lives under it, which is
            # what `build/` means; the regex already covers the `(?:/.*)?` tail.
            if rule.regex.match(normalized):
                decision = rule
        if decision is None or decision.negated:
            return None
        return decision.source

    def is_ignored(self, path: str) -> bool:
        return self.matched_rule(path) is not None


def build_matcher(foxignore_text: Optional[str]) -> IgnoreMatcher:
    """Repository rules when a `.foxignore` exists, otherwise the built-in defaults.

    A committed `.foxignore` replaces the defaults outright rather than extending them,
    so a repository that genuinely needs to track a `.zip` can say so without having to
    out-negate a list it cannot see.
    """
    if foxignore_text is None:
        return IgnoreMatcher.default()
    matcher = IgnoreMatcher.from_text(foxignore_text)
    return matcher if matcher else IgnoreMatcher.default()


DEFAULT_FOXIGNORE_TEMPLATE = "\n".join(
    [
        "# .foxignore — paths Zanbeel must never store.",
        "# Same syntax as .gitignore: globs, ! to re-include, / to anchor, trailing / for directories.",
        "# This file replaces the built-in defaults, so keep the entries you still want.",
        "",
        "# Version control and tooling metadata",
        ".fox/",
        ".git/",
        "",
        "# Dependencies and virtual environments",
        "node_modules/",
        "venv/",
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        "",
        "# Build output",
        "build/",
        "dist/",
        "out/",
        "target/",
        "obj/",
        "",
        "# Archives and installers — these are what bloat a repository",
        "*.zip",
        "*.tar.gz",
        "*.7z",
        "*.exe",
        "*.msi",
        "*.apk",
        "*.iso",
        "",
        "# Local noise",
        ".DS_Store",
        "Thumbs.db",
        "*.log",
        "",
    ]
)
