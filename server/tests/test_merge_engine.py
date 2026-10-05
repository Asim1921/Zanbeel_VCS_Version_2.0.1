"""Tests for the line-level three-way merge every merge path relies on.

The merge used to be whole-file: two edits anywhere in one file were a conflict. These pin
down the Git behaviour that replaced it -- separate edits combine, overlapping and adjacent
ones conflict -- and the byte-for-byte promises (line endings, final newline) that keep a
merge from rewriting content nobody changed.
"""
import os
import sys
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

from app.services.merge import (  # noqa: E402
    CONFLICT_ADD_ADD, CONFLICT_BINARY, CONFLICT_CONTENT, CONFLICT_MODIFY_DELETE,
    _merge_text, _merge_trees, conflict_kind, has_conflict_markers, merge_text, split_lines,
)

BASE = b"one\ntwo\nthree\nfour\nfive\nsix\n"


def edit(text: bytes, old: bytes, new: bytes) -> bytes:
    assert old in text
    return text.replace(old, new, 1)


class LineMergeTests(unittest.TestCase):
    def test_edits_to_different_regions_combine(self):
        ours = edit(BASE, b"one\n", b"ONE\n")
        theirs = edit(BASE, b"six\n", b"SIX\n")
        result = merge_text(BASE, ours, theirs)
        self.assertEqual(result.conflicts, 0)
        self.assertEqual(result.content, b"ONE\ntwo\nthree\nfour\nfive\nSIX\n")

    def test_overlapping_edits_conflict_on_that_region_only(self):
        ours = edit(BASE, b"three\n", b"three-main\n")
        theirs = edit(BASE, b"three\n", b"three-feature\n")
        result = merge_text(BASE, ours, theirs, ("main", "base", "feature"))
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(
            result.content.decode(),
            "one\ntwo\n"
            "<<<<<<< main\nthree-main\n"
            "||||||| base\nthree\n"
            "=======\nthree-feature\n"
            ">>>>>>> feature\n"
            "four\nfive\nsix\n",
        )

    def test_adjacent_edits_conflict_as_in_git(self):
        ours = edit(BASE, b"two\n", b"TWO\n")
        theirs = edit(BASE, b"three\n", b"THREE\n")
        self.assertEqual(merge_text(BASE, ours, theirs).conflicts, 1)

    def test_the_same_change_on_both_sides_is_taken_once(self):
        ours = edit(BASE, b"four\n", b"FOUR\n")
        result = merge_text(BASE, ours, ours)
        self.assertEqual(result.conflicts, 0)
        self.assertEqual(result.content, ours)

    def test_one_side_unchanged_takes_the_other(self):
        theirs = edit(BASE, b"two\n", b"2\n") + b"seven\n"
        result = merge_text(BASE, BASE, theirs)
        self.assertEqual((result.conflicts, result.content), (0, theirs))

    def test_insertions_at_different_places_combine(self):
        ours = b"zero\n" + BASE
        theirs = BASE + b"seven\n"
        result = merge_text(BASE, ours, theirs)
        self.assertEqual(result.conflicts, 0)
        self.assertEqual(result.content, b"zero\n" + BASE + b"seven\n")

    def test_different_insertions_at_the_same_place_conflict(self):
        ours = edit(BASE, b"three\n", b"three\nmain-line\n")
        theirs = edit(BASE, b"three\n", b"three\nfeature-line\n")
        result = merge_text(BASE, ours, theirs)
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(result.hunks[0]["base"], "")

    def test_several_conflicts_are_reported_separately(self):
        ours = edit(edit(BASE, b"one\n", b"1m\n"), b"six\n", b"6m\n")
        theirs = edit(edit(BASE, b"one\n", b"1f\n"), b"six\n", b"6f\n")
        result = merge_text(BASE, ours, theirs)
        self.assertEqual(result.conflicts, 2)
        self.assertEqual([h["index"] for h in result.hunks], [0, 1])
        self.assertEqual(result.hunks[0]["start_line"], 1)

    def test_hunks_describe_each_side(self):
        ours = edit(BASE, b"five\n", b"five-main\n")
        theirs = edit(BASE, b"five\n", b"five-feature\n")
        hunk = merge_text(BASE, ours, theirs).hunks[0]
        self.assertEqual((hunk["ours"], hunk["base"], hunk["theirs"]),
                         ("five-main\n", "five\n", "five-feature\n"))


class ByteFidelityTests(unittest.TestCase):
    def test_crlf_line_endings_survive_a_clean_merge(self):
        base = b"a\r\nb\r\nc\r\nd\r\n"
        result = merge_text(base, b"A\r\nb\r\nc\r\nd\r\n", b"a\r\nb\r\nc\r\nD\r\n")
        self.assertEqual(result.content, b"A\r\nb\r\nc\r\nD\r\n")

    def test_conflict_markers_use_the_file_line_ending(self):
        base = b"a\r\nb\r\nc\r\n"
        result = merge_text(base, b"a\r\nX\r\nc\r\n", b"a\r\nY\r\nc\r\n")
        self.assertIn(b"<<<<<<< ours\r\n", result.content)
        # No bare LF anywhere: every line, markers included, ends in CRLF.
        self.assertNotIn(b"\n", result.content.replace(b"\r\n", b""))

    def test_a_missing_final_newline_is_preserved(self):
        result = merge_text(b"a\nb\nc", b"A\nb\nc", b"a\nb\nC")
        self.assertEqual(result.content, b"A\nb\nC")

    def test_a_conflict_at_the_end_without_newline_keeps_markers_on_their_own_lines(self):
        text = merge_text(b"a\nb", b"a\nB1", b"a\nB2").content.decode()
        for marker in ("<<<<<<< ours\n", "||||||| base\n", "=======\n", ">>>>>>> theirs\n"):
            self.assertIn(marker, text)
        self.assertIn("B1\n|||||||", text)

    def test_split_lines_round_trips_exactly(self):
        for text in ("", "x", "x\n", "a\r\nb\n\nc", "form\x0cfeed\nline"):
            self.assertEqual("".join(split_lines(text)), text)

    def test_non_ascii_text_merges_intact(self):
        base = "سلام\nmiddle\nfin\n".encode()
        ours = "سلام دنیا\nmiddle\nfin\n".encode()
        theirs = "سلام\nmiddle\nfin ✓\n".encode()
        result = merge_text(base, ours, theirs)
        self.assertEqual(result.content.decode(), "سلام دنیا\nmiddle\nfin ✓\n")

    def test_legacy_tuple_shape_still_works(self):
        content, conflicted = _merge_text(b"a\n", b"b\n", b"c\n")
        self.assertTrue(conflicted)
        self.assertIn(b"<<<<<<<", content)


class TreeMergeTests(unittest.TestCase):
    def test_separate_edits_in_one_file_no_longer_conflict(self):
        merged, conflicts = _merge_trees(
            {"app.py": BASE},
            {"app.py": edit(BASE, b"one\n", b"ONE\n")},
            {"app.py": edit(BASE, b"six\n", b"SIX\n")},
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(merged["app.py"], b"ONE\ntwo\nthree\nfour\nfive\nSIX\n")

    def test_a_deletion_on_one_side_is_applied(self):
        merged, conflicts = _merge_trees({"gone": b"x", "kept": b"y"},
                                         {"kept": b"y"}, {"gone": b"x", "kept": b"y"})
        self.assertEqual((merged, conflicts), ({"kept": b"y"}, []))

    def test_modify_delete_conflicts_and_keeps_the_surviving_side(self):
        merged, conflicts = _merge_trees({"f": b"old"}, {}, {"f": b"edited"})
        self.assertEqual(conflicts, ["f"])
        self.assertEqual(merged["f"], b"edited")

    def test_binary_conflict_keeps_ours(self):
        merged, conflicts = _merge_trees({"img": b"\x00base"}, {"img": b"\xff\xfeours"},
                                         {"img": b"\xff\xfetheirs"})
        self.assertEqual(conflicts, ["img"])
        self.assertEqual(merged["img"], b"\xff\xfeours")

    def test_labels_reach_the_markers(self):
        merged, _ = _merge_trees({"f": b"a\n"}, {"f": b"b\n"}, {"f": b"c\n"},
                                 ("main", "base", "topic"))
        self.assertIn(b"<<<<<<< main\n", merged["f"])
        self.assertIn(b">>>>>>> topic\n", merged["f"])


class ClassificationTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(conflict_kind(b"a", None, b"b"), CONFLICT_MODIFY_DELETE)
        self.assertEqual(conflict_kind(b"a", b"b", None), CONFLICT_MODIFY_DELETE)
        self.assertEqual(conflict_kind(None, b"a", b"b"), CONFLICT_ADD_ADD)
        self.assertEqual(conflict_kind(b"a", b"\xff\xfe", b"b"), CONFLICT_BINARY)
        self.assertEqual(conflict_kind(b"a", b"b", b"c"), CONFLICT_CONTENT)

    def test_marker_detection_needs_a_complete_block(self):
        self.assertTrue(has_conflict_markers(merge_text(b"a\n", b"b\n", b"c\n").content))
        self.assertTrue(has_conflict_markers(b"<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> topic\n"))
        # A Markdown underline is not a conflict.
        self.assertFalse(has_conflict_markers(b"Title\n=======\nbody\n"))
        self.assertFalse(has_conflict_markers(b"<<<<<<< only an opener\n"))
        self.assertFalse(has_conflict_markers(None))
        self.assertFalse(has_conflict_markers(b"\xff\xfe binary"))


if __name__ == "__main__":
    unittest.main()
