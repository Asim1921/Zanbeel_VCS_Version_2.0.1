"""Three-way merging for every operation that combines two lines of history.

Pull request merges, branch merges, cherry-pick, revert, rebase and the conflict resolver
all come through here, so this is the one definition of "these two changes can be
combined".

The merge used to be whole-file: if both sides touched a file anywhere, the entire file
became one conflict, even when one side edited the top and the other the bottom. That is
not how Git behaves and it turned routine parallel work into manual resolution. This is a
line-level diff3 merge instead:

* the base is aligned against each side separately, and every base line both sides kept
  untouched is a fixed point;
* between fixed points, a region changed by one side takes that side, a region changed
  identically by both is taken once, and a region changed differently by both is a
  conflict;
* adjacent edits conflict, exactly as in Git -- two changes with no untouched line
  between them cannot be ordered safely.

Lines keep their own endings, so a merge never rewrites CRLF as LF or adds a newline the
file did not have, except immediately before a conflict marker.
"""

import base64
import re

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

#: Above this many lines across the three sides the alignment is skipped and the file is
#: compared whole. SequenceMatcher is quadratic in the worst case; a generated file of
#: half a million lines must not hold a request hostage.
MAX_MERGE_LINES = 60000

DEFAULT_LABELS = ("ours", "base", "theirs")

# A complete marker block: opening, separator and closing lines in that order. A lone
# "=======" (a Markdown underline, say) is not a conflict, so all three are required.
_MARKER_BLOCK = re.compile(
    r"^<{7}(?: [^\n]*)?\r?\n.*?^={7}\r?\n.*?^>{7}(?: [^\n]*)?\r?$",
    re.MULTILINE | re.DOTALL,
)

CONFLICT_CONTENT = "content"
CONFLICT_ADD_ADD = "add_add"
CONFLICT_MODIFY_DELETE = "modify_delete"
CONFLICT_BINARY = "binary"


def _try_decode_text(content: Optional[bytes]) -> Optional[str]:
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except Exception:
        return None


def split_lines(text: str) -> List[str]:
    """Lines with their endings attached, so ``"".join`` gives the exact text back.

    Splits on ``\\n`` only. ``str.splitlines`` also breaks on form feeds, vertical tabs
    and Unicode separators, which would make a merge rewrite bytes it never meant to.
    """
    if not text:
        return []
    pieces = text.split("\n")
    lines = [piece + "\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def _alignment(base: Sequence[str], side: Sequence[str]) -> Dict[int, int]:
    """For each base line the side kept unchanged, its index in the side."""
    # autojunk off: with it on, any line that appears in more than 1% of a long file --
    # blank lines, closing braces -- is ignored for matching, which scatters the
    # alignment and manufactures conflicts.
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    mapping: Dict[int, int] = {}
    for base_start, side_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapping[base_start + offset] = side_start + offset
    return mapping


@dataclass
class Region:
    """One stretch of the merged file: settled lines, or a conflict between the sides."""

    kind: str  # "clean" or "conflict"
    lines: List[str] = field(default_factory=list)
    base: List[str] = field(default_factory=list)
    ours: List[str] = field(default_factory=list)
    theirs: List[str] = field(default_factory=list)


def merge_regions(base: Sequence[str], ours: Sequence[str],
                  theirs: Sequence[str]) -> List[Region]:
    """The diff3 merge of three line sequences, as alternating clean and conflict regions."""
    regions: List[Region] = []

    def settle(lines: Sequence[str]) -> None:
        if not lines:
            return
        if regions and regions[-1].kind == "clean":
            regions[-1].lines.extend(lines)
        else:
            regions.append(Region("clean", lines=list(lines)))

    def reconcile(base_seg, ours_seg, theirs_seg) -> None:
        if not (base_seg or ours_seg or theirs_seg):
            return
        if ours_seg == theirs_seg:
            settle(ours_seg)          # unchanged, or the same change made twice
        elif ours_seg == base_seg:
            settle(theirs_seg)        # only their side changed this region
        elif theirs_seg == base_seg:
            settle(ours_seg)          # only our side changed this region
        else:
            regions.append(Region(
                "conflict", base=list(base_seg), ours=list(ours_seg), theirs=list(theirs_seg),
            ))

    if len(base) + len(ours) + len(theirs) > MAX_MERGE_LINES:
        reconcile(base, ours, theirs)
        return regions

    to_ours = _alignment(base, ours)
    to_theirs = _alignment(base, theirs)

    b = o = t = 0
    for index, line in enumerate(base):
        if index not in to_ours or index not in to_theirs:
            continue
        # A base line both sides kept: everything before it is one region to reconcile.
        # Matching blocks are monotonic in both sequences, so these never run backwards.
        o_at, t_at = to_ours[index], to_theirs[index]
        reconcile(base[b:index], ours[o:o_at], theirs[t:t_at])
        settle([line])
        b, o, t = index + 1, o_at + 1, t_at + 1
    reconcile(base[b:], ours[o:], theirs[t:])
    return regions


@dataclass
class MergeResult:
    content: bytes
    conflicts: int
    hunks: List[Dict[str, object]]


def _line_ending(*sides: Sequence[str]) -> str:
    for lines in sides:
        for line in lines:
            if line.endswith("\r\n"):
                return "\r\n"
            if line.endswith("\n"):
                return "\n"
    return "\n"


def render(regions: Sequence[Region], labels: Sequence[str] = DEFAULT_LABELS,
           line_ending: str = "\n") -> Tuple[str, List[Dict[str, object]]]:
    """Merged text with Git diff3-style markers, plus a description of each conflict."""
    ours_label, base_label, theirs_label = labels
    out: List[str] = []
    hunks: List[Dict[str, object]] = []

    def emit_side(lines: Sequence[str]) -> None:
        out.extend(lines)
        # A side ending without a newline (the end of the file) would otherwise run
        # straight into the next marker line.
        if lines and not lines[-1].endswith("\n"):
            out.append(line_ending)

    for region in regions:
        if region.kind == "clean":
            out.extend(region.lines)
            continue
        if out and not out[-1].endswith("\n"):
            out.append(line_ending)
        start_line = sum(chunk.count("\n") for chunk in out) + 1
        out.append(f"<<<<<<< {ours_label}{line_ending}")
        emit_side(region.ours)
        out.append(f"||||||| {base_label}{line_ending}")
        emit_side(region.base)
        out.append(f"======={line_ending}")
        emit_side(region.theirs)
        out.append(f">>>>>>> {theirs_label}{line_ending}")
        hunks.append({
            "index": len(hunks),
            "start_line": start_line,
            "ours": "".join(region.ours),
            "base": "".join(region.base),
            "theirs": "".join(region.theirs),
        })

    return "".join(out), hunks


def merge_text(base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes],
               labels: Optional[Sequence[str]] = None) -> MergeResult:
    """Line-level three-way merge of one text file. Missing sides count as empty."""
    base_lines = split_lines(_try_decode_text(base) or "")
    ours_lines = split_lines(_try_decode_text(ours) or "")
    theirs_lines = split_lines(_try_decode_text(theirs) or "")

    regions = merge_regions(base_lines, ours_lines, theirs_lines)
    text, hunks = render(
        regions, labels or DEFAULT_LABELS, _line_ending(ours_lines, theirs_lines, base_lines),
    )
    return MergeResult(content=text.encode("utf-8"), conflicts=len(hunks), hunks=hunks)


def _merge_text(base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes],
                labels: Optional[Sequence[str]] = None) -> Tuple[bytes, bool]:
    """``merge_text`` in the original (content, conflicted) shape."""
    result = merge_text(base, ours, theirs, labels)
    return result.content, result.conflicts > 0


def conflict_kind(base: Optional[bytes], ours: Optional[bytes],
                  theirs: Optional[bytes]) -> str:
    """What sort of decision a conflicted path needs."""
    if ours is None or theirs is None:
        return CONFLICT_MODIFY_DELETE
    if _try_decode_text(ours) is None or _try_decode_text(theirs) is None:
        return CONFLICT_BINARY
    if base is None:
        return CONFLICT_ADD_ADD
    return CONFLICT_CONTENT


def has_conflict_markers(content: Optional[bytes]) -> bool:
    """Whether text still contains a complete, unresolved conflict block."""
    text = _try_decode_text(content)
    return bool(text) and _MARKER_BLOCK.search(text) is not None


def _merge_trees(base: Dict[str, bytes], ours: Dict[str, bytes], theirs: Dict[str, bytes],
                 labels: Optional[Sequence[str]] = None) -> Tuple[Dict[str, bytes], List[str]]:
    """Merge three snapshots. Returns the merged snapshot and the conflicted paths.

    A conflicted text path carries its marker-annotated merge; a modify/delete conflict
    keeps the side that still has the file (as Git leaves it in the work tree); a binary
    conflict keeps ours. Callers that cannot accept conflicts must check the list.
    """
    merged: Dict[str, bytes] = {}
    conflicts: List[str] = []
    for path in sorted(set(base) | set(ours) | set(theirs)):
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

        kind = conflict_kind(base_content, our_content, their_content)
        if kind == CONFLICT_MODIFY_DELETE:
            merged[path] = our_content if our_content is not None else their_content
            conflicts.append(path)
        elif kind == CONFLICT_BINARY:
            merged[path] = our_content
            conflicts.append(path)
        else:
            result = merge_text(base_content, our_content, their_content, labels)
            merged[path] = result.content
            if result.conflicts:
                conflicts.append(path)

    return merged, conflicts


merge_trees = _merge_trees


def _tree_to_commit_payload(tree: Dict[str, bytes]) -> Dict[str, str]:
    return {
        path: base64.b64encode(content).decode("utf-8")
        for path, content in tree.items()
    }
