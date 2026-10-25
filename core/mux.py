"""Auto-Forge-MKV / core/mux.py

封装层：把「纯净视频」和「准备好的音频」拼成成品 MKV。

============================================================
mkvmerge 的选项作用域（这是最容易出事的地方）
============================================================
mkvmerge 的选项只作用于「它后面那个输入文件」。
所以 `video --no-audio` 是给下一个文件用的，不是给 video 用的 ——
写成 `video --no-audio` 既不生效也不报错，原音轨照样被拷进成品。

正确写法：
    ["--no-audio", video]          ← 选项在前，文件在后
    ["--sync", "0:1130", audio]    ← 同理，紧贴音频文件

============================================================
延迟只能在一处生效
============================================================
    方案 A：audio.py 用 adelay 把静音写进样本 → 这里 --sync 必须是 0
    方案 B：audio.py 只转码不延迟      → 这里 --sync 0:<delay_ms>

两者同时用 = 延迟翻倍。MuxSpec 里有断言盯着，不许绕过。

============================================================
mkvmerge 退出码
============================================================
    0 = 成功   1 = 有警告（文件可用）   2 = 失败
把 1 当失败会让一堆可用的成品被丢掉；把 2 当成功会留下坏文件。
所以 ok_codes=(0, 1)，并且退出码 1 要打警告 + 走回读校验。
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .util import (
        FFPROBE, MKVMERGE, cleanup, ensure_parent, have, human_size,
        run, to_float,
    )
except ImportError:  # 直接 python core/mux.py 运行时
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.util import (
        FFPROBE, MKVMERGE, cleanup, ensure_parent, have, human_size,
        run, to_float,
    )

log = logging.getLogger("forge")


# ---------------------------------------------------------------- 规格
@dataclass
class MuxSpec:
    """封装规格。

    delay_ms        容器层偏移。音频已物理延迟时**必须为 0**。
    audio_delayed   audio.py 是否已经把静音写进样本（方案 A）。
    language        音轨语言标签。
    track_name      音轨显示名，如 "OP" / "Main Audio"。
    default_audio   是否把这条音轨设为默认轨。
    fps_declare     VFR 源才需要声明；CFR 源加了反而会引入漂移。
    chapters        章节 XML/OGM 路径，None 表示不带章节。
    stop_after_video 音频比视频长时截断到视频末尾，避免尾部黑屏。
    """
    delay_ms: int = 0
    audio_delayed: bool = False
    language: str = "jpn"
    track_name: str = ""
    default_audio: bool = True
    fps_declare: float | None = None
    chapters: str | None = None
    stop_after_video: bool = True

    def __post_init__(self) -> None:
        if self.audio_delayed and self.delay_ms:
            raise ValueError(
                f"音频已在样本层延迟过，容器偏移必须为 0；"
                f"否则实际延迟会变成 {self.delay_ms * 2}ms"
            )

    @property
    def container_sync_ms(self) -> int:
        """真正交给 --sync 的数值。"""
        return 0 if self.audio_delayed else self.delay_ms


# ---------------------------------------------------------------- 命令拼装
def build_mux_cmd(video: str, audio: str, out: str,
                  spec: MuxSpec | None = None) -> list[str]:
    spec = spec or MuxSpec()
    cmd = [
        MKVMERGE,
        "--ui-language", "en",              # 强制英文，防中文 Windows 输出编码崩
        "--no-date",                        # 结果可复现
        "--disable-track-statistics-tags",  # 去掉统计标签，否则每次输出都不同
        "-o", out,
    ]

    if spec.fps_declare:
        # 只对 VFR 源用；CFR 源加了会引入漂移
        cmd += ["--default-duration", f"0:{spec.fps_declare}"]

    # --- 视频输入：选项必须在文件名之前
    cmd += ["--no-audio", "--no-subtitles", "--no-buttons"]
    if not spec.chapters:
        cmd += ["--no-chapters"]
    cmd += [video]

    # --- 音频输入：--sync / 标签同样紧贴文件
    cmd += ["--sync", f"0:{spec.container_sync_ms}"]
    cmd += ["--language", f"0:{spec.language}"]
    cmd += ["--default-track-flag", f"0:{'yes' if spec.default_audio else 'no'}"]
    if spec.track_name:
        cmd += ["--track-name", f"0:{spec.track_name}"]
    if spec.chapters:
        cmd += ["--chapters", spec.chapters]
    cmd += [audio]

    return cmd


# ---------------------------------------------------------------- 回读
def _probe_json(path: str, entries: list[tuple[str, str]], desc: str) -> dict | None:
    """每次只查一类东西。

    ★ 旧版把两条 -show_entries 塞进同一条命令（packet=... 和 format=...），
      ffprobe 会返回空 JSON，于是校验永远判失败、成品被静默删掉。
      这里强制「一次一查」，从根上消掉那个坑。
    """
    cmd = [FFPROBE, "-v", "error"]
    for k, v in entries:
        cmd += ["-show_entries", f"{k}={v}"]
    cmd += ["-of", "json", path]
    code, out, err = run(cmd, desc=desc)
    if code != 0 or not out.strip():
        log.error("❌ %s 失败: %s", desc, (err or "").strip()[:200])
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        log.error("❌ %s 输出不是 JSON: %s", desc, e)
        return None


def first_packet_pts(path: str, stream: str = "a:0") -> float | None:
    """取某条流第一个包的 pts（秒）。取不到返回 None，不猜 0。"""
    cmd = [FFPROBE, "-v", "error", "-select_streams", stream,
           "-read_intervals", "%+#1", "-show_entries", "packet=pts_time",
           "-of", "csv=p=0", path]
    code, out, _ = run(cmd, desc=f"首包 pts({stream})")
    if code != 0 or not out.strip():
        return None
    return to_float(out.strip().splitlines()[0].split(",")[0], None)


def stream_layout(path: str) -> dict | None:
    """成品里到底有几条视频轨 / 几条音轨，音轨是什么参数。"""
    d = _probe_json(path, [(
        "stream",
        "index,codec_type,codec_name,sample_rate,channels,"
        "bits_per_raw_sample,bits_per_sample,disposition",
    )], "轨道清点")
    if d is None:
        return None
    streams = d.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    return {"videos": videos, "audios": audios, "all": streams}


def container_duration(path: str) -> float:
    d = _probe_json(path, [("format", "duration,size")], "容器时长")
    if not d:
        return 0.0
    return to_float((d.get("format") or {}).get("duration"), 0.0) or 0.0


# ---------------------------------------------------------------- 校验
def verify_mux(path: str, spec: MuxSpec | None = None,
               expect_duration: float | None = None,
               min_rate: int = 48000, min_bits: int = 24) -> bool:
    """回读成品：轨道数、音轨参数、音轨首包 pts、时长。

    位深校验故意放宽：FLAC 在 MKV 里 bits_per_raw_sample 常常是 0 或空，
    硬拿它当判据会误杀一堆合格成品。真要看位深得靠 bits_per_sample。
    """
    spec = spec or MuxSpec()
    if not Path(path).is_file():
        log.error("❌ 成品不存在: %s", path)
        return False
    size = Path(path).stat().st_size
    if size < 4096:
        log.error("❌ 成品只有 %d 字节", size)
        return False

    layout = stream_layout(path)
    if layout is None:
        return False

    ok = True
    nv, na = len(layout["videos"]), len(layout["audios"])
    if nv != 1:
        log.error("❌ 视频轨 %d 条（应为 1；多半是封面图混进来了）", nv)
        ok = False
    if na != 1:
        log.error("❌ 音轨 %d 条（应为 1；原音轨没剔除？）", na)
        ok = False

    if layout["audios"]:
        a = layout["audios"][0]
        if a.get("codec_name") != "flac":
            log.error("❌ 音轨不是 FLAC: %s", a.get("codec_name"))
            ok = False
        rate = int(a.get("sample_rate") or 0)
        if rate < min_rate:
            log.error("❌ 采样率 %d < %d，Hi-Res 金标拿不到", rate, min_rate)
            ok = False
        bits = int(a.get("bits_per_raw_sample") or a.get("bits_per_sample") or 0)
        if bits and bits < min_bits:
            log.error("❌ 位深 %dbit < %dbit", bits, min_bits)
            ok = False
        elif not bits:
            log.info("ℹ️ 位深字段为空（FLAC 在 MKV 里常见），跳过位深硬校验")
        log.info("🎵 音轨: %s %dHz/%dch", a.get("codec_name"), rate,
                 int(a.get("channels") or 0))

    # 延迟方向验证：容器偏移时，音轨首包 pts 应 ≈ delay/1000
    pts = first_packet_pts(path, "a:0")
    expect_pts = spec.container_sync_ms / 1000.0
    if pts is None:
        log.warning("⚠️ 取不到音轨首包 pts，跳过延迟校验")
    elif abs(pts - expect_pts) > 0.15:
        log.error("❌ 音轨首包 pts=%.3fs，期望 %.3fs → --sync 位置或正负号错了",
                  pts, expect_pts)
        ok = False
    else:
        log.info("✅ 音轨首包 pts=%.3fs（期望 %.3fs）", pts, expect_pts)

    dur = container_duration(path)
    if expect_duration and expect_duration > 0 and dur > 0:
        if abs(dur - expect_duration) > 1.0:
            log.error("❌ 成品 %.3fs，预期 %.3fs", dur, expect_duration)
            ok = False
        else:
            log.info("✅ 时长 %.3fs ≈ 预期 %.3fs", dur, expect_duration)

    if ok:
        log.info("✅ 封装校验通过: 1 视频 + 1 FLAC | %s | %s",
                 human_size(size), path)
    return ok


# ---------------------------------------------------------------- 执行
def mux(video: str, audio: str, out: str, spec: MuxSpec | None = None,
        timeout: int = 1800, verify: bool = True,
        expect_duration: float | None = None) -> bool:
    spec = spec or MuxSpec()
    for p in (video, audio):
        if not Path(p).is_file():
            log.error("❌ 输入不存在: %s", p)
            return False
    if not have(MKVMERGE):
        log.error("❌ 找不到 mkvmerge（试过 PATH 与 MKVToolNix 默认安装目录）")
        return False
    if Path(out).exists():
        log.warning("⚠️ 覆盖已存在成品: %s", out)

    ensure_parent(out)
    cmd = build_mux_cmd(video, audio, out, spec)
    code, _, err = run(cmd, desc="mkvmerge 封装",
                       ok_codes=(0, 1), timeout=timeout)

    if code not in (0, 1):
        log.error("❌ 封装失败（退出码 %s）: %s", code, err.strip()[:400])
        cleanup(out)
        return False
    if code == 1:
        tail = err.strip().splitlines()[-1][:200] if err.strip() else ""
        log.warning("⚠️ mkvmerge 有警告（成品通常可用）: %s", tail)

    if verify and not verify_mux(out, spec, expect_duration):
        cleanup(out)
        return False
    return True


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import argparse

    try:
        from .util import setup_logging
    except ImportError:
        from core.util import setup_logging

    setup_logging()
    ap = argparse.ArgumentParser(description="Auto-Forge-MKV 封装层")
    ap.add_argument("video")
    ap.add_argument("audio")
    ap.add_argument("--out", default="_out/muxed.mkv")
    ap.add_argument("--delay", type=int, default=0, help="容器偏移毫秒")
    ap.add_argument("--delayed", action="store_true",
                    help="音频已物理延迟过（--delay 会强制为 0）")
    ap.add_argument("--name", default="")
    ap.add_argument("--lang", default="jpn")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()

    spec = MuxSpec(delay_ms=a.delay, audio_delayed=a.delayed,
                   language=a.lang, track_name=a.name)
    print("命令:", " ".join(build_mux_cmd(a.video, a.audio, a.out, spec)))
    if not a.run:
        print("（未执行，加 --run 才真跑）")
        sys.exit(0)
    sys.exit(0 if mux(a.video, a.audio, a.out, spec) else 1)
