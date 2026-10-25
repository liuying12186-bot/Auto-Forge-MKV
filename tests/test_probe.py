"""Auto-Forge-MKV / tests/test_probe.py

用标准库 unittest 写，不强制安装 pytest；装了 pytest 也能直接收集。
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.probe import (  # noqa: E402
    _parse_fps, _to_float, _to_int, pick_cutpoint,
)


class TestHelpers(unittest.TestCase):
    def test_parse_fps_fraction(self):
        self.assertAlmostEqual(_parse_fps("24000/1001"), 23.976, places=2)
        self.assertEqual(_parse_fps("60"), 60.0)

    def test_parse_fps_garbage(self):
        self.assertEqual(_parse_fps("0/0"), 0.0)
        self.assertEqual(_parse_fps(""), 0.0)
        self.assertEqual(_parse_fps(None), 0.0)

    def test_to_int_handles_na(self):
        self.assertEqual(_to_int("24"), 24)
        self.assertEqual(_to_int(None), 0)
        self.assertEqual(_to_int("N/A"), 0)

    def test_to_float_filters_nan(self):
        self.assertEqual(_to_float("1.5"), 1.5)
        self.assertEqual(_to_float(float("nan")), 0.0)
        self.assertEqual(_to_float(None), 0.0)


KF = [0.0, 4.167, 8.333, 12.5]


class TestPickCutpoint(unittest.TestCase):
    """语义铁律：头切用 after，尾切用 before，反过来会丢内容或留残影。"""

    def test_after_for_head_cut(self):
        self.assertEqual(pick_cutpoint(KF, 3.0, "after"), 4.167)
        self.assertEqual(pick_cutpoint(KF, 4.167, "after"), 4.167)

    def test_before_for_tail_cut(self):
        self.assertEqual(pick_cutpoint(KF, 10.0, "before"), 8.333)
        self.assertEqual(pick_cutpoint(KF, 100.0, "before"), 12.5)

    def test_after_beyond_end_clamps(self):
        self.assertEqual(pick_cutpoint(KF, 999.0, "after"), 12.5)

    def test_before_below_start_clamps(self):
        self.assertEqual(pick_cutpoint(KF, -5.0, "before"), 0.0)

    def test_empty_returns_none(self):
        self.assertIsNone(pick_cutpoint([], 1.0))


MEDIA = os.environ.get("FORGE_TEST_MEDIA", "")


@unittest.skipUnless(MEDIA and os.path.exists(MEDIA),
                     "需要设置 FORGE_TEST_MEDIA 指向一份真实视频")
class TestRealMedia(unittest.TestCase):
    def test_probe_real_media(self):
        from core.probe import list_keyframes, probe_streams

        m = probe_streams(MEDIA)
        self.assertGreater(m.width, 0)
        self.assertGreater(m.duration, 0)

        kfs = list_keyframes(MEDIA)
        self.assertTrue(kfs)
        self.assertEqual(kfs, sorted(kfs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
