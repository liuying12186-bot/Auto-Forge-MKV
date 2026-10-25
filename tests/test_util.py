"""Auto-Forge-MKV / tests/test_util.py

覆盖 core/util.py 里纯函数的部分：时间格式化、数值解析、大小换算。
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.util import (  # noqa: E402
    fmt_dur, fmt_sec, human_size, mask_secret, parse_fps, to_float, to_int,
)


class TestFmtDur(unittest.TestCase):
    """fmt_dur 必须逐级进位，不能出现 00:00:60.000 这种秒数。"""

    def test_normal(self):
        self.assertEqual(fmt_dur(0), "00:00:00.000")
        self.assertEqual(fmt_dur(75.5), "00:01:15.500")
        self.assertEqual(fmt_dur(3725.25), "01:02:05.250")

    def test_second_rounds_up_to_minute(self):
        self.assertEqual(fmt_dur(59.9999), "00:01:00.000")
        self.assertEqual(fmt_dur(3599.9999), "01:00:00.000")

    def test_minute_rounds_up_to_hour(self):
        self.assertEqual(fmt_dur(7199.9999), "02:00:00.000")

    def test_invalid(self):
        self.assertEqual(fmt_dur(None), "00:00:00.000")
        self.assertEqual(fmt_dur(-1), "00:00:00.000")


class TestFmtSec(unittest.TestCase):
    def test_no_scientific_notation(self):
        # 1e-07 绝不能走 str(float)，否则会变成 1e-07
        self.assertEqual(fmt_sec(1e-07), "0.000000")
        self.assertEqual(fmt_sec(1e-07, ms=True), "0.000")

    def test_precision(self):
        self.assertEqual(fmt_sec(1.5), "1.500000")
        self.assertEqual(fmt_sec(1.5, ms=True), "1.500")
        self.assertEqual(fmt_sec(None), "0.000000")


class TestNumbers(unittest.TestCase):
    def test_parse_fps(self):
        self.assertAlmostEqual(parse_fps("24000/1001"), 23.976, places=2)
        self.assertEqual(parse_fps("60"), 60.0)
        self.assertEqual(parse_fps("0/0"), 0.0)
        self.assertEqual(parse_fps(""), 0.0)

    def test_to_float_nan_and_default(self):
        self.assertEqual(to_float("1.5"), 1.5)
        self.assertEqual(to_float(float("nan")), 0.0)
        self.assertIsNone(to_float(float("nan"), default=None))
        self.assertEqual(to_float("N/A", default=0.0), 0.0)

    def test_to_int(self):
        self.assertEqual(to_int("24"), 24)
        self.assertEqual(to_int(None), 0)
        self.assertEqual(to_int("N/A", default=-1), -1)


class TestHumanSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_size(512), "512B")
        self.assertEqual(human_size(1024), "1.0KB")
        self.assertEqual(human_size(1536), "1.5KB")
        self.assertEqual(human_size(1024 ** 3), "1.0GB")

    def test_huge_clamps_to_tb(self):
        self.assertTrue(human_size(1024 ** 5 * 8).endswith("TB"))


class TestMaskSecret(unittest.TestCase):
    def test_short_is_fully_hidden(self):
        self.assertEqual(mask_secret("abc"), "***")
        self.assertEqual(mask_secret(""), "(空)")
        self.assertEqual(mask_secret(None), "(空)")

    def test_long_keeps_head_tail(self):
        self.assertEqual(mask_secret("sk-1234567890abcdef"), "sk-123…cdef")


if __name__ == "__main__":
    unittest.main(verbosity=2)
