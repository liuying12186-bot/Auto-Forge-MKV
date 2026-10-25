"""Auto-Forge-MKV / tests/test_batch.py

core/batch.py 的目录扫描逻辑：扩展名过滤、递归开关、排序稳定性、输出路径。
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.batch import VIDEO_EXTS, collect_sources, out_path_for, parse_exts  # noqa: E402


class TestParseExts(unittest.TestCase):
    def test_default_when_empty(self):
        self.assertIs(parse_exts(None), VIDEO_EXTS)
        self.assertIs(parse_exts(""), VIDEO_EXTS)

    def test_normalize(self):
        got = parse_exts("mp4, .MKV ;mov")
        self.assertEqual(got, frozenset({".mp4", ".mkv", ".mov"}))

    def test_blank_items_ignored(self):
        self.assertEqual(parse_exts(" , mp4 ,, "), frozenset({".mp4"}))


class TestCollect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "b.mp4").write_bytes(b"x")
        (self.root / "a.mkv").write_bytes(b"x")
        (self.root / "note.txt").write_text("x", encoding="utf-8")
        (self.root / "song.flac").write_bytes(b"x")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "c.mov").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_flat_by_default(self):
        got = [p.name for p in collect_sources(self.root)]
        self.assertEqual(got, ["a.mkv", "b.mp4"])

    def test_recursive(self):
        got = [p.relative_to(self.root).as_posix()
               for p in collect_sources(self.root, recursive=True)]
        self.assertEqual(got, ["a.mkv", "b.mp4", "sub/c.mov"])

    def test_non_video_skipped(self):
        names = [p.name for p in collect_sources(self.root, recursive=True)]
        self.assertNotIn("note.txt", names)
        self.assertNotIn("song.flac", names)

    def test_custom_exts(self):
        got = collect_sources(self.root, exts=frozenset({".flac"}))
        self.assertEqual([p.name for p in got], ["song.flac"])

    def test_missing_dir(self):
        self.assertEqual(collect_sources(self.root / "nope"), [])

    def test_stable_order(self):
        first = collect_sources(self.root, recursive=True)
        second = collect_sources(self.root, recursive=True)
        self.assertEqual(first, second)


class TestOutPath(unittest.TestCase):
    def test_keeps_subdir(self):
        root = Path("/media/clips")
        out = Path("/media/clips/_out")
        got = out_path_for(root / "sub" / "op.mkv", root, out)
        self.assertEqual(got.as_posix(), "/media/clips/_out/sub/op_forge.mkv")

    def test_top_level(self):
        root = Path("/media/clips")
        got = out_path_for(root / "op.mkv", root, Path("/media/clips/_out"))
        self.assertEqual(got.as_posix(), "/media/clips/_out/op_forge.mkv")

    def test_same_name_different_dir_not_collide(self):
        root = Path("/x")
        a = out_path_for(root / "s1" / "op.mkv", root, Path("/x/_out"))
        b = out_path_for(root / "s2" / "op.mkv", root, Path("/x/_out"))
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
