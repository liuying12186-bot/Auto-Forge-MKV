"""Auto-Forge-MKV / core/cut.py

切割层：接在 probe 之后，负责「算出切在哪里」并「真的切出来」。

============================================================
为什么必须吸附关键帧
============================================================
ffmpeg -c copy 是无损的，但它只能在关键帧（I 帧）处起头。
如果陛下要 12.0s 切，而最近的关键帧在 16.667s，结果就只能从 16.667s
开始 —— 早切那几秒会连 UI 一起留下，或者反过来把正片开头吃掉。
所以本模块先把请求时间吸附到关键帧，并把实际偏移量明白报出来，
绝不悄悄切错。

============================================================
★ 2026/10/25 实测：单条命令不可靠，必须分两段
============================================================
素材：魔女4k-Hires (4).mkv  3840x2160 HEVC 120fps，flac 192k 音轨，
      关键帧间隔 10.042s（长度 92.941s）

  写法                                          结果
  -i SRC -ss 10.042 -t 5                        653 字节空文件 ✗
  -i SRC -ss 10.042 -to 15.042                  空文件 ✗
  -i SRC -ss 10.042            （不加 -t）       82.899s ✓
  -ss 10.042 -i SRC -t 5                        15.100s（多出 10s 的 GOP）✗
  -ss 10.042 -i SRC -to 15.042                  25.125s ✗
  -i SRC -t 5                   （不 seek）       5.041s ✓

  两步走：
  step1  -i SRC -ss 10.042 -c copy → tmp（82.899s）
  step2  -i tmp -t 5 -c copy       → out（5.059s）✓

结论：**不要把 -ss 和 -t 放进同一条 -c copy 命令**。
在这类「音轨 DURATION 缺失 + 附封面图 + 超长 GOP」的容器上，
ffmpeg 会算出 0 个可输出包，还不报错（退出码 0），静默产出空文件。
所以本模块固定用两段式：先切头拿到新基准，再在新基准上截时长。

============================================================
映射规则（会真的失败的地方）
============================================================
-map 0 会让附封面图（mjpeg/png）作为第二条 video stream 混进成品，
还会带上 tmcd / bin_data 这类元数据轨，-c copy 时直接报
"Could not find tag for codec ..."，整条命令失败。
因此固定 -map 0:v:0，并且显式 -sn -dn -map_chapters -1。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .probe import (
        FFPROBE, FFMPEG, VideoInfo, nearest_keyframe, pick_cutpoint,
        probe_audio_info, probe_keyframes, probe_video_info, run,
    )
except ImportError:  # 直接 python core/cut.py 运行时
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.probe import (
        FFPROBE, FFMPEG, VideoInfo, nearest_keyframe, pick_cutpoint,
        probe_audio_info, probe_keyframes, probe_video_info, run,
    )

log = logging.getLogger("forge")

# ffmpeg 允许的最小可切时长，短于此值视为参数错误
MIN_DURATION = 0.05


# ---------------------------------------------------------------- 数据结构
@dataclass
class CutPlan:
    """一份完整的切割方案。dry_run 时就是给人看的施工图。"""
    src: str
    out: str
    req_start: float                       # 陛下想要的起点（秒）
    req_end: float                         # 陛下想要的终点（秒），0 表示到片尾
    real_start: float                      # 吸附后的实际起点
    real_end: float                        # 吸附后的实际终点
    keyframes: list[float] = field(default_factory=list)
    cmds: list[list[str]] = field(default_factory=list)   # 两段命令
    tmp: str = ""                          # 第一段的中间产物

    @property
    def start_offset(self) -> float:
        """实际起点比请求起点晚了多少秒。正的 = 多留了开头。"""
        return round(self.real_start - self.req_start, 3)

    @property
    def end_offset(self) -> float:
        """实际终点与请求终点之差，负的 = 提前收了。"""
        if not self.req_end:
            return 0.0
        return round(self.real_end - self.req_end, 3)

    @property
    def duration(self) -> float:
        return round(self.real_end - self.real_start, 3)

    @property
    def cmd(self) -> list[str]:
        """兼容旧接口：返回最后一段命令（真正产成品的那条）。"""
        return self.cmds[-1] if self.cmds else []

    def report(self) -> str:
        lines = [
            f"📼 源文件 : {self.src}",
            f"📦 输出   : {self.out}",
            f"🎯 请求区间: {self.req_start:.3f}s → "
            + (f"{self.req_end:.3f}s" if self.req_end else "片尾"),
            f"✂️ 实际落点: {self.real_start:.3f}s → {self.real_end:.3f}s "
            f"(共 {self.duration:.3f}s)",
        ]
        if abs(self.start_offset) > 1e-6:
            lines.append(f"↔️ 开头偏移: {self.start_offset:+.3f}s（受关键帧限制）")
        if abs(self.end_offset) > 1e-6:
            lines.append(f"↔️ 结尾偏移: {self.end_offset:+.3f}s（受关键帧限制）")
        for i, c in enumerate(self.cmds, 1):
            lines.append(f"🛠 第{i}段: " + " ".join(c))
        return "\n".join(lines)


# ---------------------------------------------------------------- 命令拼装
def _out_opts(extra: list[str] | None = None) -> list[str]:
    """所有产出命令共用的收尾参数。"""
    opts = [
        "-map", "0:v:0",          # 只取主视频；封面图/时间码轨一律不进
        "-an", "-sn", "-dn",
        "-c:v", "copy",
        "-map_metadata", "0",     # 元数据（语言/标题）保留
        "-map_chapters", "-1",    # 章节按旧时间轴，切完必然错位，丢掉
    ]
    if extra:
        opts += extra
    return opts


def build_cut_cmds(
    src: str, out: str, start: float, end: float,
    accurate: bool = True, audio_codec: str = "copy",
    tmp: str | None = None,
) -> list[list[str]]:
    """拼两段 ffmpeg 命令。

    第 1 段：只切头。`-i SRC -ss START`，不加 -t。
    第 2 段：在新基准上截时长。`-i TMP -t DUR`。

    accurate=False 时改用输入侧 -ss（快，但结尾会多出一个 GOP，
    只适合生成预览；实测这份素材会多吃 10.042s）。
    """
    dur = round(end - start, 6)
    if dur <= 0:
        raise ValueError(f"裁剪时长必须为正: start={start} end={end}")

    src_p = Path(src)
    if tmp is None:
        # 中间产物放在输出旁边而非源目录，避免污染片库根目录
        out_p = Path(out)
        tmp = str(out_p.with_name(f".{out_p.stem}.seg1.mkv"))

    seg1 = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if accurate:
        # 先打开文件再定位：实测这条能落到正确包，不产生空文件
        seg1 += ["-i", src, "-ss", f"{start:.6f}"]
    else:
        seg1 += ["-ss", f"{start:.6f}", "-i", src]
    seg1 += _out_opts()
    seg1 += [tmp]

    seg2 = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", tmp, "-t", f"{dur:.6f}"]
    seg2 += _out_opts(["-avoid_negative_ts", "make_zero"])
    if audio_codec != "copy" and not accurate:
        # audio 只在需要转码时才动；默认整个音轨由后续 mux 提供
        seg2 += ["-c:a", audio_codec]
    seg2 += [out]

    return [seg1, seg2]


def build_cut_cmd(src: str, out: str, start: float, end: float,
                  accurate: bool = True, audio_codec: str = "copy",
                  tmp: str | None = None) -> list[str]:
    """兼容旧接口：返回第一段命令（切头那一条）。"""
    return build_cut_cmds(src, out, start, end, accurate, audio_codec, tmp)[0]


# ---------------------------------------------------------------- 落点计算
def plan_cut(
    src: str,
    out: str | None = None,
    start: float = 0.0,
    end: float = 0.0,
    accurate: bool = True,
    audio_codec: str = "copy",
) -> CutPlan | None:
    """算出一份切割方案。不执行，只规划。"""
    v: VideoInfo | None = probe_video_info(src)
    if v is None:
        log.error("无法探测视频信息，放弃规划: %s", src)
        return None

    keyframes = probe_keyframes(src)
    if not keyframes:
        log.error("没有探测到关键帧（可能是图片或非常规封装），放弃规划。")
        return None

    total = v.duration or keyframes[-1]
    req_end = end if end and end > 0 else total

    if start < 0 or req_end <= start:
        log.error("区间非法: start=%.3f end=%.3f", start, req_end)
        return None

    # 起点吸附：取 >= start 的第一个关键帧（头切用 after）
    real_start = pick_cutpoint(keyframes, start, side="after")
    # 终点吸附：取 <= req_end 的最后一个关键帧（尾切用 before）
    real_end = pick_cutpoint(keyframes, req_end, side="before")

    if real_start is None or real_end is None or real_end <= real_start:
        log.error("吸附后区间为空：可能切得太短（短于一个 GOP）。")
        return None

    if real_end - real_start < MIN_DURATION:
        log.error("吸附后只剩 %.3fs，短于 %.2fs 下限。", real_end - real_start, MIN_DURATION)
        return None

    if out is None:
        base, ext = os.path.splitext(src)
        out = f"{base}_cut{ext or '.mkv'}"

    cmds = build_cut_cmds(src, out, real_start, real_end, accurate, audio_codec)
    return CutPlan(
        src=src, out=out,
        req_start=start, req_end=req_end if end else 0.0,
        real_start=real_start, real_end=real_end,
        keyframes=keyframes, cmds=cmds, tmp=cmds[0][-1],
    )


# ---------------------------------------------------------------- 校验
def verify_output(path: str, expect: float, tol: float = 0.6) -> bool:
    """回读成品：时长要对，首包 pts 要接近 0。

    退出码 0 只说明 ffmpeg 没骂人，不说明文件里有东西 ——
    上面那种 653 字节的空壳在 -loglevel error 下是完全静默的。
    """
    if not Path(path).is_file():
        log.error("❌ 输出文件不存在: %s", path)
        return False
    size = Path(path).stat().st_size
    if size < 4096:
        log.error("❌ 输出只有 %d 字节，基本是空壳", size)
        return False

    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", path]
    code, out, _ = run(cmd, desc="时长校验")
    if code != 0:
        return False
    try:
        dur = float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        log.error("❌ 无法解析输出时长: %r", out[:80])
        return False

    if abs(dur - expect) > tol:
        log.error("❌ 输出 %.3fs，预期 %.3fs，差 %.3fs", dur, expect, dur - expect)
        return False

    # 首包 pts：分两条查询，避免 -show_entries 重复导致 ffprobe 返回空
    pts_cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-read_intervals", "%+#1", "-show_entries", "packet=pts_time",
               "-of", "csv=p=0", path]
    c2, o2, _ = run(pts_cmd, desc="首包校验")
    pts = 0.0
    if c2 == 0 and o2.strip():
        try:
            pts = float(o2.strip().splitlines()[0].split(",")[0])
        except ValueError:
            pts = 0.0
    if abs(pts) > 0.1:
        log.error("❌ 首包 pts=%.4f ≠ 0，后续音频对齐会整体偏移", pts)
        return False

    log.info("✅ 裁剪校验通过: %.3fs，首包 pts=%.4f", dur, pts)
    return True


# ---------------------------------------------------------------- 执行
def execute(plan: CutPlan, timeout: int = 1800, keep_tmp: bool = False) -> bool:
    """跑完两段命令。任何一步失败都会清掉残骸，不留半成品。"""
    if not plan.cmds:
        log.error("没有可执行的命令")
        return False

    for i, cmd in enumerate(plan.cmds, 1):
        code, _, err = run(cmd, desc=f"无损切割 第{i}段", timeout=timeout)
        if code != 0:
            log.error("第%d段失败（退出码 %s）: %s", i, code, err.strip()[:400])
            _cleanup(plan.out, plan.tmp, keep_tmp)
            return False

    if not verify_output(plan.out, plan.duration):
        _cleanup(plan.out, plan.tmp, keep_tmp)
        return False

    _cleanup(plan.out, plan.tmp, keep_tmp, only_tmp=True)
    return True


def _cleanup(out: str, tmp: str, keep_tmp: bool, only_tmp: bool = False) -> None:
    """失败时删掉 -y 建出来的半成品，避免下次批量跑被误当成品。"""
    targets = [tmp] if only_tmp else [out, tmp]
    if keep_tmp:
        targets = [t for t in targets if t != tmp]
    for p in targets:
        if p and Path(p).is_file():
            try:
                Path(p).unlink()
                log.debug("已清理: %s", p)
            except OSError as e:
                log.warning("清理失败 %s: %s", p, e)


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Auto-Forge-MKV 切割层")
    ap.add_argument("src", nargs="?",
                    default=r"C:\Users\11243\Downloads\恋ひ恋ふ縁 (Game Size).mkv")
    ap.add_argument("--start", type=float, default=3.0, help="起点秒")
    ap.add_argument("--end", type=float, default=100.0, help="终点秒，0=片尾")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fast", action="store_true", help="用输入侧 -ss（预览用，结尾会松）")
    ap.add_argument("--audio", default="copy", help="copy 或 flac")
    ap.add_argument("--run", action="store_true", help="真的执行，不加只看方案")
    a = ap.parse_args()

    plan = plan_cut(a.src, a.out, a.start, a.end, not a.fast, a.audio)
    if plan is None:
        sys.exit(1)

    print(plan.report())
    print(f"🔑 全部关键帧 {len(plan.keyframes)} 个: "
          f"{[round(k, 3) for k in plan.keyframes]}")
    if not a.run:
        print("（未执行，加 --run 才真切）")
        sys.exit(0)
    sys.exit(0 if execute(plan) else 1)
