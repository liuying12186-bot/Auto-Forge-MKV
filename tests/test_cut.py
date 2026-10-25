"""Auto-Forge-MKV / tests/test_cut.py

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cut import build_cut_cmd, build_cut_cmds  # noqa: E402


class TestTwoStageCut(unittest.TestCase):
    """铁律：-ss 与 -t 绝不能出现在同一条 -c copy 命令里。

    2026/10/25 实测（魔女4k-Hires (4).mkv，120fps，GOP=10.042s）：
        -i SRC -ss 10.042 -t 5  → 653 字节空文件，退出码却是 0
    所以必须是「先切头 / 再截尾」两段。
    """

    def test_two_commands_returned(self):
        cmds = build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333)
        self.assertEqual(len(cmds), 2)

    def test_ss_and_t_never_in_same_command(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333):
            self.assertFalse("-ss" in cmd and "-t" in cmd,
                             "同一条命令里出现 -ss 和 -t 会静默产出空文件")

    def test_first_stage_only_seeks(self):
        seg1 = build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333)[0]
        self.assertIn("-ss", seg1)
        self.assertNotIn("-t", seg1)
        self.assertIn("4.167000", seg1)

    def test_second_stage_only_truncates(self):
        seg2 = build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333)[1]
        self.assertIn("-t", seg2)
        self.assertNotIn("-ss", seg2)

    def test_duration_value_on_second_stage(self):
        seg2 = build_cut_cmds("S.mkv", "O.mkv", 10.0, 15.0)[1]
        self.assertEqual(seg2[seg2.index("-t") + 1], "5.000000")

    def test_accurate_puts_ss_after_input(self):
        seg1 = build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333, accurate=True)[0]
        self.assertLess(seg1.index("-i"), seg1.index("-ss"))

    def test_fast_puts_ss_before_input(self):
        seg1 = build_cut_cmds("S.mkv", "O.mkv", 4.167, 98.333, accurate=False)[0]
        self.assertLess(seg1.index("-ss"), seg1.index("-i"))


class TestMapping(unittest.TestCase):
    """-map 0 会带进封面图和 tmcd 轨，导致 -c copy 直接失败。"""

    def test_only_first_video_mapped(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0):
            self.assertEqual(cmd[cmd.index("-map") + 1], "0:v:0")

    def test_drops_subs_attach_and_chapters(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0):
            self.assertIn("-sn", cmd)
            self.assertIn("-dn", cmd)
            self.assertIn("-an", cmd)
            self.assertEqual(cmd[cmd.index("-map_chapters") + 1], "-1")

    def test_video_always_copy(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0):
            self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")

    def test_seconds_use_fixed_point(self):
        """浮点 repr 会产出 1e-07 这种写法，不同 ffmpeg 版本行为不稳。"""
        cmd = build_cut_cmds("S.mkv", "O.mkv", 1e-07, 5.0)[0]
        self.assertIn("0.000000", cmd)


class TestCommandShape(unittest.TestCase):
    def test_output_is_last(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0):
            self.assertTrue(cmd[-1].endswith(".mkv"))

    def test_second_stage_normalizes_ts(self):
        seg2 = build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0)[1]
        self.assertEqual(seg2[seg2.index("-avoid_negative_ts") + 1], "make_zero")

    def test_no_stdin_so_batch_never_hangs(self):
        for cmd in build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0):
            self.assertIn("-nostdin", cmd)

    def test_negative_duration_rejected(self):
        with self.assertRaises(ValueError):
            build_cut_cmds("S.mkv", "O.mkv", 10.0, 5.0)

    def test_compat_wrapper_returns_first_stage(self):
        self.assertEqual(
            build_cut_cmd("S.mkv", "O.mkv", 0.0, 5.0),
            build_cut_cmds("S.mkv", "O.mkv", 0.0, 5.0)[0],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
