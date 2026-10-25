"""Auto-Forge-MKV / cli/main.py

单文件流水线：probe → cut → audio → mux → verify

    python -m cli.main forge <源视频> [--start 秒] [--end 秒]
                              [--audio 外部音轨] [--delay 毫秒]
                              [--dry-run] [--keep]

设计约束（每一条都是踩过坑之后写死的）
--------------------------------------------------------------------
1. --dry-run 打印全部命令但一条都不执行，方便先看施工图。
2. 每步产物落 .forge_cache/，失败时保留现场便于继续调。
3. 任一步失败立即中断，并清掉这一步的残骸 —— 但不清上游产物。
4. 退出码：0 成功，1 某步失败，2 参数错误，3 缺依赖。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 允许 python cli/main.py 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.__init__ import __version__  # noqa: E402
from core.audio import AudioSpec, build_audio_cmd, prepare_audio  # noqa: E402
from core.batch import collect_sources, out_path_for, parse_exts  # noqa: E402
from core.cut import build_cut_cmds, execute as cut_execute, plan_cut  # noqa: E402
from core.mux import MuxSpec, build_mux_cmd, mux  # noqa: E402
from core.probe import list_keyframes, load_or_probe  # noqa: E402
from core.util import (  # noqa: E402
    FFMPEG, MKVMERGE, ensure_parent, ensure_utf8_console, fmt_dur, human_size,
    init_tools, setup_logging,
)

log = logging.getLogger("forge")

CACHE = Path(".forge_cache")


# ---------------------------------------------------------------- 结果
@dataclass
class StageResult:
    name: str
    ok: bool
    seconds: float = 0.0
    detail: str = ""
    skipped: bool = False   # 跳过 ≠ 失败：dry-run 探测不到就标跳过


@dataclass
class RunReport:
    src: str
    out: str
    stages: list[StageResult] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages if not s.skipped)

    def add(self, name: str, ok: bool, seconds: float, detail: str = "",
            skipped: bool = False) -> None:
        self.stages.append(StageResult(name, ok, seconds, detail, skipped))

    def summary(self) -> str:
        lines = ["", "=" * 62, "📋 流水线汇总", "=" * 62]
        for s in self.stages:
            mark = "⏭️" if s.skipped else ("✅" if s.ok else "❌")
            lines.append(f"{mark} {s.name:<14} {s.seconds:6.2f}s  {s.detail}")
        total = sum(s.seconds for s in self.stages)
        lines.append("-" * 62)
        lines.append(f"{'总计':<16} {total:6.2f}s")
        if self.artifacts:
            lines.append("")
            for k, v in self.artifacts.items():
                lines.append(f"  {k:<8} {v}")
        if self.ok:
            p = Path(self.out)
            if p.is_file():
                lines.append(f"\n🎬 成品: {p}  ({human_size(p.stat().st_size)})")
        lines.append("=" * 62)
        return "\n".join(lines)


# ---------------------------------------------------------------- 参数
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="forge", description="Auto-Forge-MKV 单文件流水线")
    ap.add_argument("src", help="源视频")
    ap.add_argument("-o", "--out", default=None, help="成品路径（默认 _out/<名字>.mkv）")
    ap.add_argument("--start", type=float, default=0.0, help="起点秒")
    ap.add_argument("--end", type=float, default=0.0, help="终点秒，0=片尾")
    ap.add_argument("--audio", default=None,
                    help="外部音轨；不给则沿用源里的音轨")
    ap.add_argument("--delay", type=int, default=0,
                    help="音频延迟毫秒，正数=音频偏晚")
    ap.add_argument("--audio-mode", choices=["physical", "container"],
                    default="physical",
                    help="physical=adelay 填真静音（默认）；container=交给 mkvmerge --sync")
    ap.add_argument("--rate", type=int, default=48000, help="音轨目标采样率")
    ap.add_argument("--bits", type=int, default=24, help="音轨目标位深")
    ap.add_argument("--name", default="", help="音轨显示名")
    ap.add_argument("--lang", default="jpn", help="音轨语言")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    ap.add_argument("--keep", action="store_true", help="保留中间产物")
    ap.add_argument("--refresh", action="store_true", help="忽略 probe 缓存")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version",
                    version=f"Auto-Forge-MKV {__version__}")
    ap.add_argument("--batch", action="store_true",
                    help="把 src 当目录，逐个处理里面的视频")
    ap.add_argument("--recursive", action="store_true",
                    help="配合 --batch：连子目录一起")
    ap.add_argument("--ext", default=None,
                    help="配合 --batch：只处理这些扩展名，如 mp4,mkv（默认常见视频容器）")
    return ap.parse_args(argv)


# ---------------------------------------------------------------- 主流程
def forge(a: argparse.Namespace) -> RunReport:
    src = a.src
    out = a.out or str(Path("_out") / (Path(src).stem + "_forge.mkv"))
    report = RunReport(src=src, out=out)

    if a.dry_run:
        log.info("🧪 dry-run：只打印命令，不执行任何操作")

    # ---------------- 1. probe
    t = time.perf_counter()
    try:
        media = load_or_probe(src, str(CACHE), refresh=a.refresh)
    except (RuntimeError, FileNotFoundError) as e:
        if a.dry_run:
            # dry-run 的目的只是给施工图；文件本身读不出来也算不上“失败”，
            # 标成跳过，退出码仍然是 0，免得批量预览时满屏红。
            report.add("probe", False, time.perf_counter() - t,
                       "dry-run 跳过（读不出媒体信息）", skipped=True)
            return report
        report.add("probe", False, time.perf_counter() - t, str(e)[:80])
        return report
    detail = (f"{media.width}x{media.height}@{media.fps:.3f} "
              f"{media.v_codec}/{media.pix_fmt}")
    if media.is_vfr:
        detail += " ⚠️VFR"
    report.add("probe", True, time.perf_counter() - t, detail)
    report.artifacts["缓存"] = str(CACHE)

    if not a.quiet:
        print(f"🎬 {media.width}x{media.height} @ {media.fps:.3f}fps "
              f"| {media.v_codec} {media.pix_fmt} | {fmt_dur(media.duration)}")
        if media.a_codec:
            print(f"🎵 源音轨 {media.a_codec} {media.a_rate}Hz/"
                  f"{media.a_bits}bit/{media.a_channels}ch "
                  f"（共 {media.a_count} 条）")

    # ---------------- 2. cut
    t = time.perf_counter()
    plan = plan_cut(src, str(CACHE / "cut.mp4"), a.start, a.end)
    if plan is None:
        report.add("cut", False, time.perf_counter() - t, "切点规划失败")
        return report

    if media.is_vfr:
        log.warning("⚠️ 源是 VFR（%.3f vs %.3f fps），-c copy 后可能出现音画漂移；"
                    "需要先转 CFR 再切", media.fps, media.avg_fps)

    if not a.quiet:
        print(f"\n✂️ 请求 {a.start:.3f}s → "
              f"{a.end if a.end else '片尾'}，"
              f"吸附到 {plan.real_start:.3f}s → {plan.real_end:.3f}s "
              f"（{plan.duration:.3f}s）")
        for i, c in enumerate(plan.cmds, 1):
            print(f"   第{i}段: {' '.join(c)}")

    if a.dry_run:
        report.add("cut", True, 0.0, "dry-run 未执行")
    elif cut_execute(plan, keep_tmp=not a.keep):
        report.add("cut", True, time.perf_counter() - t,
                   f"{plan.duration:.3f}s → {plan.out}")
        report.artifacts["cut"] = plan.out
    else:
        report.add("cut", False, time.perf_counter() - t, "切割失败")
        return report

    # ---------------- 3. audio
    t = time.perf_counter()
    audio_src = a.audio or src
    physical = a.audio_mode == "physical"
    # 音轨若取自源视频，就必须和画面切同一段窗口；外部音轨默认已对齐，不截
    same_source = a.audio is None
    spec = AudioSpec(
        sample_rate=a.rate, bits=a.bits,
        delay_ms=a.delay if physical else 0,
        trim_start=plan.real_start if same_source else 0.0,
        trim_dur=plan.duration if same_source else 0.0,
    )

    if not media.a_codec and not a.audio:
        report.add("audio", False, 0.0, "源里没有音轨，也没给 --audio")
        return report

    try:
        cmd = build_audio_cmd(audio_src, str(CACHE / "audio.flac"), spec)
        if not a.quiet:
            print(f"\n🎧 音频{'物理延迟' if physical else '容器偏移'} "
                  f"{a.delay:+d}ms | {' '.join(cmd)}")
    except Exception as e:  # noqa: BLE001
        report.add("audio", False, time.perf_counter() - t, str(e)[:80])
        return report

    audio_out = str(CACHE / "audio.flac")
    if a.dry_run:
        report.add("audio", True, 0.0, "dry-run 未执行")
    elif prepare_audio(audio_src, audio_out, spec):
        report.add("audio", True, time.perf_counter() - t,
                   f"flac {spec.sample_rate}Hz/{spec.bits}bit")
        report.artifacts["audio"] = audio_out
    else:
        report.add("audio", False, time.perf_counter() - t, "音频处理失败")
        return report

    # ---------------- 4. mux
    t = time.perf_counter()
    mux_spec = MuxSpec(
        delay_ms=0 if physical else a.delay,
        audio_delayed=physical and a.delay != 0,
        language=a.lang, track_name=a.name,
        fps_declare=None,          # VFR 已在上一步警告，不在这里偷偷声明
    )
    mux_video = plan.out if not a.dry_run else str(CACHE / "cut.mp4")
    mux_audio = audio_out if not a.dry_run else audio_out

    if not a.quiet:
        print(f"\n📦 封装: {' '.join(build_mux_cmd(mux_video, mux_audio, out, mux_spec))}")

    if a.dry_run:
        report.add("mux", True, 0.0, "dry-run 未执行")
        return report

    expect = plan.duration if plan.duration > 0 else None
    if mux(mux_video, mux_audio, out, mux_spec, expect_duration=expect):
        report.add("mux", True, time.perf_counter() - t,
                   Path(out).name)
        report.artifacts["成品"] = out
    else:
        report.add("mux", False, time.perf_counter() - t, "封装失败")
        return report

    # ---------------- 5. 清理
    if not a.keep:
        for f in (str(CACHE / "audio.flac"), mux_video):
            p = Path(f)
            if p.is_file() and p != Path(out):
                try:
                    p.unlink()
                except OSError:
                    pass
    return report


# ---------------------------------------------------------------- 批处理
def run_batch(a: argparse.Namespace) -> int:
    """把一个目录里的视频逐个跑一遍，单个失败不拖累其他。

    退出码沿用单文件的约定：0 全成功，1 有失败，2 参数/目录问题。
    """
    root = Path(a.src)
    if not root.is_dir():
        log.error("❌ --batch 需要一个目录: %s", a.src)
        return 2

    files = collect_sources(root, parse_exts(a.ext), recursive=a.recursive)
    if not files:
        log.error("❌ 目录里没找到可处理的视频: %s", a.src)
        return 2

    out_dir = Path(a.out) if a.out else root / "_out"
    log.info("📂 批处理 %d 个文件 → %s", len(files), out_dir)
    if a.dry_run:
        log.info("🧪 dry-run：只列清单，不执行")

    done, failed = [], []
    t0 = time.perf_counter()
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root).as_posix()
        log.info("—— [%d/%d] %s", i, len(files), rel)
        sub = argparse.Namespace(**vars(a))  # 浅拷贝，别污染循环变量
        sub.src = str(f)
        sub.batch = False
        sub.out = str(out_path_for(f, root, out_dir))
        try:
            report = forge(sub)
        except Exception as e:  # 一个文件炸了不该让整批停住
            log.error("❌ %s 异常: %s", rel, e)
            failed.append((rel, "异常"))
            continue
        if report.ok:
            done.append(rel)
        else:
            why = next((s.detail for s in report.stages if not s.ok), "失败")
            failed.append((rel, why))

    cost = time.perf_counter() - t0
    log.info("========== 批处理结束 ==========")
    log.info("✅ 成功 %d / ❌ 失败 %d / ⏱ %.2fs", len(done), len(failed), cost)
    if failed:
        for rel, why in failed:
            log.warning("  · %s —— %s", rel, why)
    return 0 if not failed else 1


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    setup_logging(quiet=a.quiet)

    tools = init_tools()
    missing = [k for k in ("ffmpeg", "ffprobe") if not Path(tools[k]).is_file()]
    if missing:
        log.error("❌ 缺少必需工具: %s", ", ".join(missing))
        return 3
    if not Path(MKVMERGE).is_file() and not a.dry_run:
        log.error("❌ 找不到 mkvmerge（装了 MKVToolNix 才有）")
        return 3

    if a.batch:
        return run_batch(a)

    if not Path(a.src).is_file():
        log.error("❌ 源文件不存在: %s", a.src)
        return 2

    ensure_parent(a.out or "_out/x.mkv")
    t0 = time.perf_counter()
    report = forge(a)
    print(report.summary())
    print(f"⏱ 墙钟 {time.perf_counter() - t0:.2f}s")
    return 0 if report.ok else 1


# ---------------------------------------------------------------- 双击友好
def _pause(msg: str = "按回车键关闭窗口…") -> None:
    """双击 exe 时，控制台窗口会跟着进程一起消失，提示一闪而过。

    所以冻结成 exe 之后，退出前停一下让人看清结果；
    被管道或脚本调用时 stdin 不是终端，直接跳过，不拖慢自动化。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        ensure_utf8_console()
        if not sys.stdin or not sys.stdin.isatty():
            return
        input(msg)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


BANNER = """Auto-Forge-MKV  ·  视频无损切段 + 音轨重封装

直接把视频文件拖到本程序图标上，就会用默认参数开始处理。
也可以用命令行指定参数：

  AutoForgeMKV.exe <源视频> [--start 秒] [--end 秒]

常用示例：
  AutoForgeMKV.exe "D:\\片子.mp4" --start 35 --end 120
  AutoForgeMKV.exe "D:\\片子.mp4" --audio "D:\\音频.flac" --delay -240

整个文件夹一起处理：
  AutoForgeMKV.exe "D:\\素材库" --batch
  AutoForgeMKV.exe "D:\\素材库" --batch --recursive --out "D:\\成品"

查看全部参数：AutoForgeMKV.exe --help
"""


def _cli() -> int:
    """真正的入口：没有任何参数时给一屏说明，而不是甩出 argparse 报错。"""
    ensure_utf8_console()
    argv = sys.argv[1:]
    if not argv:
        print(BANNER)
        _pause()
        return None
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except SystemExit as e:  # argparse --help / --version / 参数错误都从这里出去
        return int(e.code or 0)


if __name__ == "__main__":
    _code = _cli()
    if _code is None:  # 只打印了 banner，提示已经自己停过了
        sys.exit(0)
    if _code == 0:
        _pause("完成，按回车键关闭窗口…")
    else:
        _pause(f"出错了（退出码 {_code}），按回车键关闭窗口…")
    sys.exit(_code)
