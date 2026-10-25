"""Auto-Forge-MKV / core/audio.py

音频层：把外部音轨准备成「能直接喂给 mkvmerge 的 FLAC」。

============================================================
这一层唯一存在的理由：延迟只能在一处生效
============================================================
音画不同步的成因有两种，处理位置也只有一个正确答案：

    方案 A（物理延迟，用 adelay 填真静音样本）
        好处：FLAC 里真的有 1130ms 静音，任何播放器、转码服务都无法忽略。
        代价：整轨重编码。

    方案 B（容器偏移，交给 mkvmerge --sync）
        好处：零耗时、零损失，改个数字重跑 3 秒出结果。
        代价：容器内没有真实静音；部分转码器重新封装时会把偏移丢掉。

本模块只实现 A，B 在 mux.py。两者**绝不能同时用**，
否则延迟会叠加成 2260ms。所以 AudioSpec 里有一条断言盯着这件事。

============================================================
Hi-Res 判定（金标不是玄学，是硬条件）
============================================================
    采样率 >= 48000 且 位深 >= 24  →  拿到 Hi-Res 标
FLAC 的位深必须写成 s32（ffmpeg 侧），容器里才记成 24bit；
用 -sample_fmt s16 会静默降成 16bit，金标当场消失且不报错。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .probe import FFMPEG, FFPROBE, MediaProbe, probe_streams, run
except ImportError:  # 直接 python core/audio.py 运行时
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.probe import FFMPEG, FFPROBE, MediaProbe, probe_streams, run

log = logging.getLogger("forge")

HIRES_MIN_RATE = 48000
HIRES_MIN_BITS = 24


# ---------------------------------------------------------------- 规格
@dataclass
class AudioSpec:
    """音频处理规格。

    delay_ms   正数 = 音频比画面晚（需要在开头填静音）。
               负数 = 音频比画面早（需要切掉开头）。单位毫秒。
    bits       目标位深，默认 24。低于 24 就拿不到 Hi-Res。
    sample_rate 目标采样率，默认 48000。
    """
    sample_rate: int = HIRES_MIN_RATE
    bits: int = HIRES_MIN_BITS
    delay_ms: int = 0
    pad_tail_sec: float = 0.0      # 音频比视频短时补尾，避免播放器提前收场
    already_delayed: bool = False  # 源文件本身已经物理延迟过
    keep_channels: bool = True
    # 从源里截取与画面同一段窗口后再做延迟。
    # ★ 2026/10/25 端到端实测踩到：音轨取自整条源（109s），视频只切了 55.8s，
    #   封装完成品 109.5s，尾部多出 54s 黑屏。音频必须和画面同窗口。
    trim_start: float = 0.0
    trim_dur: float = 0.0          # 0 = 不截断

    @property
    def has_trim(self) -> bool:
        return self.trim_dur > 0 or self.trim_start > 0

    def __post_init__(self) -> None:
        # 延迟只能在一处生效：源已物理延迟，就别再加一次
        if self.already_delayed and self.delay_ms:
            raise ValueError(
                "already_delayed=True 时 delay_ms 必须为 0，"
                f"否则延迟会叠加成 {self.delay_ms * 2}ms"
            )

    @property
    def is_hires(self) -> bool:
        return self.sample_rate >= HIRES_MIN_RATE and self.bits >= HIRES_MIN_BITS


# ---------------------------------------------------------------- 判断
def needs_convert(src: MediaProbe, spec: AudioSpec) -> bool:
    """源音频是否已经满足规格（满足就可以 -c:a copy，零损失）。"""
    if not src.a_codec:
        return True
    if src.a_codec != "flac":
        return True
    if src.a_rate < spec.sample_rate:
        return True
    if src.a_bits and src.a_bits < spec.bits:
        return True
    if spec.delay_ms:
        return True          # 要填静音，必须动
    if spec.pad_tail_sec:
        return True
    if spec.has_trim:
        return True
    return False


def build_filter(spec: AudioSpec, channels: int = 2) -> str:
    """拼音频滤镜链。顺序有讲究：先截窗口，再填头，最后补尾。"""
    parts: list[str] = []

    if spec.has_trim:
        # 截取与画面完全相同的窗口；asetpts 重置基准，否则 atrim 之后
        # 时间戳还是源里的绝对位置，adelay 会叠在一个奇怪起点上
        seg = f"atrim=start={spec.trim_start:.6f}"
        if spec.trim_dur > 0:
            seg += f":duration={spec.trim_dur:.6f}"
        parts.append(seg)
        parts.append("asetpts=PTS-STARTPTS")

    if spec.delay_ms < 0:
        # 负延迟：音频提前，切掉开头 |delay| ms
        cut = abs(spec.delay_ms) / 1000.0
        parts.append(f"atrim=start={cut:.6f}")
        parts.append("asetpts=PTS-STARTPTS")

    if spec.delay_ms > 0:
        # 正延迟：开头填真静音。all=1 让所有声道统一，省得声道间错位
        parts.append(f"adelay={spec.delay_ms}:all=1")
        # ★ adelay 会把 PTS 整体后移（首帧 pts=delay），必须重置回 0，
        #   否则后面的 atrim 按绝对时间轴裁，会多切掉一个 delay 的量。
        parts.append("asetpts=PTS-STARTPTS")

    if spec.pad_tail_sec > 0:
        pad_ms = int(round(spec.pad_tail_sec * 1000))
        parts.append(f"apad=pad_dur={pad_ms}ms")

    # 填完静音后总长会多出 delay，必须在尾部再裁回窗口长度，
    # 否则成品会比画面长 1.13s，播放器尾部多出一段黑屏。
    if spec.has_trim and spec.delay_ms > 0:
        parts.append(f"atrim=duration={spec.trim_dur:.6f}")
        parts.append("asetpts=PTS-STARTPTS")

    return ",".join(parts)


# ---------------------------------------------------------------- 命令拼装
def build_audio_cmd(src: str, out: str, spec: AudioSpec,
                    channels: int = 2, copy_ok: bool = True) -> list[str]:
    """拼 ffmpeg 命令。能 copy 就 copy，不能才重编码成 FLAC。"""
    probe = probe_streams(src, require_video=False)
    can_copy = copy_ok and not needs_convert(probe, spec)

    cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-map", "0:a:0", "-vn", "-sn", "-dn"]

    if can_copy:
        cmd += ["-c:a", "copy"]
    else:
        filt = build_filter(spec, channels or probe.a_channels)
        if filt:
            cmd += ["-af", filt]
        cmd += [
            "-c:a", "flac",
            # s32 = 24bit 容器记录；写成 s16 会静默降位深，Hi-Res 金标当场没了
            "-sample_fmt", "s32",
            "-ar", str(spec.sample_rate),
        ]
        if spec.keep_channels and probe.a_channels:
            cmd += ["-ac", str(probe.a_channels)]

    cmd += ["-map_metadata", "-1", out]
    return cmd


# ---------------------------------------------------------------- 源自检
def audio_source_ok(path: str, probe_sec: float = 5.0) -> tuple[bool, str]:
    """开跑前先确认源音轨真的能读出数据。

    2026/10/25 实测踩到的坑：魔女4k-Hires (4).mkv 里那条 flac 流，
    容器头的 DURATION 写的是 00:00:00，整轨转出来只有 8KB / 1.13s，
    ffmpeg 退出码却是 0 —— 源本身是坏的，不是滤镜算错。
    这种文件必须在读第一帧之前就拦下来，否则后面所有校验都在追鬼。
    """
    cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
           "-read_intervals", f"%+{probe_sec:g}",
           "-show_entries", "packet=pts_time,size", "-of", "csv=p=0", path]
    code, out, err = run(cmd, desc="音频源自检")
    if code != 0:
        return False, f"ffprobe 读不出音轨: {(err or '').strip()[:160]}"

    rows = [ln for ln in out.splitlines() if ln.strip()]
    if not rows:
        return False, "音轨前 %.0f 秒内没有任何可读包（容器时长多半是 0）" % probe_sec

    total = 0
    for ln in rows:
        parts = ln.split(",")
        if len(parts) >= 2 and parts[1].isdigit():
            total += int(parts[1])
    if total < 1024:
        return False, f"前 %.0f 秒音轨只读到 {len(rows)} 个包 / {total} 字节，源音轨疑似损坏" % (
            probe_sec, )
    return True, f"前 {probe_sec:g}s 读到 {len(rows)} 个包 / {total} 字节"


# ---------------------------------------------------------------- 校验
def packet_scan_duration(path: str, default: float = 0.0) -> float:
    """扫全部音轨包，取 max(pts + duration)。只读包不解码，很快。"""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "packet=pts_time,duration_time",
           "-of", "csv=p=0", path]
    code, out, _ = run(cmd, desc="音轨时长(packet)")
    if code != 0 or not out.strip():
        return default
    end = default
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            end = max(end, float(parts[0]) + float(parts[1]))
        except ValueError:
            continue
    return end


def audio_duration(path: str, default: float = 0.0) -> float:
    """取「容器」时长。FLAC 的容器头经常没有 overall duration（ffprobe 报 0），
    所以三级回退：format → stream → 扫包累加。
    """
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", path]
    code, out, _ = run(cmd, desc="音频时长(format)")
    if code == 0 and out.strip():
        try:
            d = float(out.strip().splitlines()[0])
            if d > 0:
                return d
        except ValueError:
            pass

    cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", path]
    code, out, _ = run(cmd, desc="音频时长(stream)")
    if code == 0 and out.strip():
        try:
            d = float(out.strip().splitlines()[0])
            if d > 0:
                return d
        except ValueError:
            pass

    # 最后手段：扫全部包取 max(pts + duration)。
    # ★ ffprobe 没有 -sseof（那是 ffmpeg 的输入选项），从尾部读只能靠
    #   -read_intervals 给起点，这里干脆扫全表；只读包不解码，一两秒就完。
    cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "packet=pts_time,duration_time",
           "-of", "csv=p=0", path]
    code, out, _ = run(cmd, desc="音频时长(packet)")
    if code == 0 and out.strip():
        end = default
        for line in out.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                end = max(end, float(parts[0]) + float(parts[1]))
            except ValueError:
                continue
        if end > 0:
            return end
    return default


def audio_stream_duration(path: str, default: float = 0.0) -> float:
    """取「音轨自己」的时长，忽略容器总时长。

    ★ 这是 audio 层必须区分的一件事：容器的 duration 按最长轨算，
      视频比音轨长时会把预期时长算大，于是校验永远差那一截。
      108.338s 的音轨装在 109.434s 的容器里，减出来正好差 1.096s。
    """
    cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", path]
    code, out, _ = run(cmd, desc="音轨时长(stream)")
    if code == 0 and out.strip():
        try:
            d = float(out.strip().splitlines()[0])
            if d > 0:
                return d
        except ValueError:
            pass
    # stream 也常是 N/A（MKV 的 flac 就是这样），直接扫包，绝不回落到容器时长
    return packet_scan_duration(path, default)


def leading_silence_ok(path: str, delay_ms: int,
                       max_db: float = -50.0) -> tuple[bool, float]:
    """证明开头真的是静音样本，而不是靠时间戳糊过去。

    这是方案 A 存在的唯一理由，所以必须实测。

    ★ 旧实现取「delay + 200ms」窗口的最大音量，结果窗口尾巴里
      含有正常音频，max_volume 永远是 -0.2dB，于是合格成品被误杀。
      现在改成用 silencedetect 量开头的静音段长度，只看它够不够长。
    """
    need = delay_ms / 1000.0
    if need <= 0:
        return True, 0.0
    span = need + 0.6
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info",
           "-i", path, "-t", f"{span:.3f}",
           "-af", f"silencedetect=noise={max_db}dB:d={max(need - 0.05, 0.05):.3f}",
           "-f", "null", "-"]
    code, _, err = run(cmd, desc="开头静音检测")
    if code != 0:
        return False, 0.0

    dur = 0.0
    start = None
    for line in err.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (ValueError, IndexError):
                start = None
        elif "silence_duration:" in line:
            try:
                dur = float(line.split("silence_duration:")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                dur = 0.0

    at_head = start is None or start <= 0.02
    return (at_head and dur >= need - 0.05), dur


def verify_audio(path: str, spec: AudioSpec, expect_duration: float | None = None,
                 tol: float = 0.5) -> bool:
    """回读成品：编码、采样率、位深、声道、开头静音、首包 pts。"""
    if not Path(path).is_file():
        log.error("❌ 音频输出不存在: %s", path)
        return False
    if Path(path).stat().st_size < 1024:
        log.error("❌ 音频输出只有 %d 字节", Path(path).stat().st_size)
        return False

    m = probe_streams(path, require_video=False)
    ok = True

    if m.a_codec != "flac":
        log.error("❌ 编码不是 FLAC: %s", m.a_codec)
        ok = False
    if m.a_rate < spec.sample_rate:
        log.error("❌ 采样率 %d < %d，拿不到 Hi-Res", m.a_rate, spec.sample_rate)
        ok = False
    # FLAC 的位深有时只出现在 bits_per_sample，probe 已经两个都试过
    if m.a_bits and m.a_bits < spec.bits:
        log.error("❌ 位深 %dbit < %dbit，金标会掉", m.a_bits, spec.bits)
        ok = False

    # 方案 A 的唯一凭证：开头必须是真静音，不是靠时间戳挪过去的
    if spec.delay_ms > 0:
        sil_ok, sil_dur = leading_silence_ok(path, spec.delay_ms)
        need = spec.delay_ms / 1000.0
        if not sil_ok:
            log.error("❌ 开头静音只有 %.3fs，要求 %.3fs——延迟没填进样本",
                      sil_dur, need)
            ok = False
        else:
            log.info("✅ 开头静音实测 %.3fs ≥ %.3fs，真静音已固化",
                     sil_dur, need)

    # 首包 pts 必须 ≈ 0：正延迟是用真样本填的，不该靠时间戳
    pts_cmd = [FFPROBE, "-v", "error", "-select_streams", "a:0",
               "-read_intervals", "%+#1", "-show_entries", "packet=pts_time",
               "-of", "csv=p=0", path]
    code, out, _ = run(pts_cmd, desc="音频首包校验")
    if code == 0 and out.strip():
        try:
            pts = float(out.strip().splitlines()[0].split(",")[0])
        except ValueError:
            pts = 0.0
        if abs(pts) > tol:
            log.error("❌ 音频首包 pts=%.3fs ≠ 0，延迟可能加在了时间戳上", pts)
            ok = False
        else:
            log.info("✅ 音频首包 pts=%.4fs（真静音填充，非容器偏移）", pts)

    dur = audio_duration(path)
    if expect_duration is not None and expect_duration > 0:
        if dur <= 0:
            log.warning("⚠️ 取不到成品时长，跳过时长校验（编码/位深已通过）")
        elif abs(dur - expect_duration) > tol:
            log.error("❌ 成品 %.3fs，预期 %.3fs（差 %+.3fs）——延迟是否真的生效？",
                      dur, expect_duration, dur - expect_duration)
            ok = False
        else:
            log.info("✅ 时长 %.3fs ≈ 预期 %.3fs（延迟已固化进样本）", dur, expect_duration)

    if ok:
        log.info("✅ 音频校验通过: flac %dHz/%dbit/%dch %.2fs",
                 m.a_rate, m.a_bits, m.a_channels, dur)
    return ok


# ---------------------------------------------------------------- 执行
def prepare_audio(src: str, out: str, spec: AudioSpec | None = None,
                  timeout: int = 3600) -> bool:
    """把音轨准备成规格要求的 FLAC。失败不留残骸。"""
    spec = spec or AudioSpec()
    if not Path(src).is_file():
        log.error("❌ 音频源不存在: %s", src)
        return False

    try:
        src_probe = probe_streams(src, require_video=False)
    except RuntimeError as e:
        log.error("%s", e)
        return False
    if not src_probe.a_codec:
        log.error("❌ 源文件里没有音频流: %s", src)
        return False

    log.info("🎧 源音频: %s %dHz/%dbit/%dch %.2fs",
             src_probe.a_codec, src_probe.a_rate,
             src_probe.a_bits, src_probe.a_channels, src_probe.duration)

    ok_src, why = audio_source_ok(src)
    if not ok_src:
        log.error("❌ 源音轨不可用: %s", why)
        log.error("   这不是滤镜或延迟参数的问题，是源文件本身坏了。")
        return False
    log.info("🔎 源自检: %s", why)

    cmd = build_audio_cmd(src, out, spec, src_probe.a_channels)
    copy_mode = cmd[cmd.index("-c:a") + 1] == "copy"
    if copy_mode:
        log.info("🎯 无需处理，直接 copy（零损失）")
    else:
        log.info("🎯 目标: flac %dHz/%dbit | 延迟 %+dms | 滤镜 %s",
                 spec.sample_rate, spec.bits, spec.delay_ms,
                 build_filter(spec, src_probe.a_channels) or "(无)")

    code, _, err = run(cmd, desc="音频准备", timeout=timeout)
    if code != 0:
        log.error("❌ 音频处理失败（退出码 %s）: %s", code, err.strip()[:400])
        _cleanup(out)
        return False

    # 预期时长：
    #   有 trim 窗口 → 窗口长度（尾部已被裁回，尺寸必须与画面一致）+ 补尾
    #   无 trim（外部音轨）→ 源音轨时长 + 延迟 + 补尾
    if spec.trim_dur > 0:
        expect = spec.trim_dur + spec.pad_tail_sec
    else:
        base = audio_stream_duration(src)
        expect = base + spec.delay_ms / 1000.0 + spec.pad_tail_sec if base > 0 else 0.0

    if not verify_audio(out, spec, expect):
        _cleanup(out)
        return False
    return True


def extract_audio(video: str, out: str, spec: AudioSpec | None = None,
                  timeout: int = 3600) -> bool:
    """从视频里取第一条音轨，走同一套处理流程。"""
    return prepare_audio(video, out, spec, timeout)


def _cleanup(path: str) -> None:
    p = Path(path)
    if p.is_file():
        try:
            p.unlink()
            log.debug("已清理: %s", path)
        except OSError as e:
            log.warning("清理失败 %s: %s", path, e)


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import argparse
    import json

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Auto-Forge-MKV 音频层")
    ap.add_argument("src")
    ap.add_argument("--out", default="_out/audio.flac")
    ap.add_argument("--rate", type=int, default=HIRES_MIN_RATE)
    ap.add_argument("--bits", type=int, default=HIRES_MIN_BITS)
    ap.add_argument("--delay", type=int, default=0, help="毫秒，正数=音频偏晚")
    ap.add_argument("--pad-tail", type=float, default=0.0, help="尾部补静音秒数")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()

    spec = AudioSpec(sample_rate=a.rate, bits=a.bits,
                     delay_ms=a.delay, pad_tail_sec=a.pad_tail)
    print(json.dumps(spec.__dict__, ensure_ascii=False, indent=2))
    print("滤镜:", build_filter(spec) or "(无)")
    if not a.run:
        print("（未执行，加 --run 才真跑）")
        sys.exit(0)
    sys.exit(0 if prepare_audio(a.src, a.out, spec) else 1)
