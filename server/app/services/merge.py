"""Three-way text and tree merging used by pull requests and rollbacks."""

import base64

from typing import Dict, List, Optional


def _try_decode_text(content: Optional[bytes]) -> Optional[str]:
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except Exception:
        return None

def _merge_text(base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes]) -> tuple[bytes, bool]:
    base_text = _try_decode_text(base) or ""
    ours_text = _try_decode_text(ours) or ""
    theirs_text = _try_decode_text(theirs) or ""
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

def _merge_trees(base: Dict[str, bytes], ours: Dict[str, bytes], theirs: Dict[str, bytes]) -> tuple[Dict[str, bytes], List[str]]:
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

        if _try_decode_text(our_content) is not None and _try_decode_text(their_content) is not None:
            merged_content, has_conflict = _merge_text(base_content, our_content, their_content)
            merged[path] = merged_content
            if has_conflict:
                conflicts.append(path)
        else:
            # Binary or undecodable conflicts - keep ours but report conflict
            if our_content is not None:
                merged[path] = our_content
            conflicts.append(path)

    return merged, conflicts


def _tree_to_commit_payload(tree: Dict[str, bytes]) -> Dict[str, str]:
    return {
        path: base64.b64encode(content).decode("utf-8")
        for path, content in tree.items()
    }
