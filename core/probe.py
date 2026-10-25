"""Auto-Forge-MKV / core/probe.py

探测层：只负责「问清楚这份文件是什么」，不改动任何数据。

对外主要接口
    probe_streams(path)      -> MediaProbe     容器/流元数据，不扫关键帧
    list_keyframes(path)     -> list[float]    关键帧时间轴
    pick_cutpoint(kfs, t, side) -> float       切点吸附
    load_or_probe(path)      -> MediaProbe     带磁盘缓存的 probe_streams

兼容旧接口（cut.py / tests 在用）
    probe_video_info / probe_audio_info / probe_keyframes / nearest_keyframe
    describe / run / _to_float / _to_int / _to_float / _f / _fps

基础设施（run / to_float / to_int / parse_fps / 工具路径）统一来自 core.util，
本模块不再自己实现一遍，避免 cut/audio/mux 为了拿一个 run() 反向依赖 probe。

关键帧扫描：默认 packet 层，失败回退 frame 层
--------------------------------------------------------------------
2026/10/25 实测（同一台机器、同一份 4K60 HEVC 素材）：

    方案            命令                                                  耗时     行数    关键帧
    packet 扫描     -show_entries packet=pts_time,flags                   0.60s    6566    29
    frame 解码      -skip_frame nokey -show_entries frame=pts_time        1.84s    29      29

两者给出的首末关键帧完全一致（0.000 / 106.667），因此默认走 packet。
但 packet 依赖 muxer 在容器里写入 K 标记（matroska 的 SimpleBlock 有
keyframe 位，MP4 的 stss 表也有），遇到不写标记的封装会返回空列表，
所以这里必须保留 frame 回退，不能只信一种。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    from .util import (  # noqa: F401  FFMPEG/MKVMERGE 供旧代码透传
        FFMPEG, FFPROBE, MKVMERGE, parse_fps, run, to_float, to_int,
    )
except ImportError:  # 直接 python core/probe.py 运行时
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.util import (  # noqa: F401
        FFMPEG, FFPROBE, MKVMERGE, parse_fps, run, to_float, to_int,
    )

log = logging.getLogger("forge")

# 兼容旧名字：早期版本用过 _to_float / _to_int / _parse_fps / _f
_to_float = to_float
_to_int = to_int
_parse_fps = parse_fps
_fps = parse_fps


def _f(x, default: float = 0.0) -> float:
    """旧接口 _f 的保底实现（mux 示例里出现过）。"""
    v = to_float(x, default)
    return default if v is None else v


# ================================================================ 数据结构
@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    pix_fmt: str = ""
    nb_frames: int = 0


@dataclass
class AudioInfo:
    duration: float
    sample_rate: int
    bits: int
    channels: int
    codec: str


@dataclass
class MediaProbe:
    """一份文件的全部元数据快照。可 JSON 序列化为缓存。"""
    path: str
    duration: float = 0.0

    v_index: int = 0
    v_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_r: str = ""              # 原始分数，如 120/1
    avg_fps: float = 0.0
    is_vfr: bool = False         # r_frame_rate 与 avg_frame_rate 差 > 0.5%
    pix_fmt: str = ""
    bit_depth: int = 8
    color: str = ""
    has_b_frames: int = 0
    start_time: float = 0.0      # 视频流起始 pts，MP4 edit list 常见 0.066667
    n_streams: int = 0

    a_codec: str = ""
    a_rate: int = 0
    a_channels: int = 0
    a_bits: int = 0
    a_count: int = 0             # 音频轨数量，供 mux 层剔除垃圾轨用

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_hires(self) -> bool:
        return self.a_rate >= 48000 and self.a_bits >= 24


# ================================================================ 流元数据
def probe_streams(path: str, timeout: int = 120,
                  require_video: bool = True) -> MediaProbe:
    """只读容器/流元数据，不扫关键帧（毫秒级返回）。

    require_video=False 时允许纯音频文件（FLAC/WAV/M4A 没有视频流），
    否则音频层一调这个函数就会拿到「源文件没有视频流」的假错误。
    附封面图（attached_pic）不算视频流，必须排除，否则 mjpeg 会被当成
    第二条 video stream，-c copy 时报 "Could not find tag for codec"。
    """
    cmd = [FFPROBE, "-v", "error", "-hide_banner",
           "-show_streams", "-show_format", "-of", "json", path]
    code, out, err = run(cmd, desc="流信息探测", timeout=timeout)
    if code != 0 or not out.strip():
        raise RuntimeError(f"ffprobe 无法读取 {path}: {(err or '').strip()[:300]}")

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出不是合法 JSON: {e}") from e

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    info = MediaProbe(path=str(Path(path).resolve()))
    info.duration = to_float(fmt.get("duration")) or 0.0
    info.n_streams = len(streams)

    audios = [s for s in streams if s.get("codec_type") == "audio"]
    info.a_count = len(audios)
    if audios:
        a = audios[0]
        info.a_codec = a.get("codec_name", "")
        info.a_rate = to_int(a.get("sample_rate"))
        info.a_channels = to_int(a.get("channels"))
        # FLAC 的位深常常只在 bits_per_raw_sample 里，两个都要试
        info.a_bits = (to_int(a.get("bits_per_raw_sample"))
                       or to_int(a.get("bits_per_sample")))

    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    if v is None:
        if require_video:
            raise RuntimeError(f"源文件没有视频流: {path}")
        return info

    info.v_index = to_int(v.get("index"))
    info.v_codec = v.get("codec_name", "")
    info.width = to_int(v.get("width"))
    info.height = to_int(v.get("height"))
    info.fps_r = v.get("r_frame_rate", "0/1")
    info.fps = parse_fps(info.fps_r)
    info.avg_fps = parse_fps(v.get("avg_frame_rate", "0/1"))
    if info.fps > 0 and info.avg_fps > 0:
        info.is_vfr = abs(info.avg_fps - info.fps) / info.fps > 0.005
    info.pix_fmt = v.get("pix_fmt", "") or ""
    info.bit_depth = (
        to_int(v.get("bits_per_raw_sample"))
        or (10 if "10" in info.pix_fmt else (12 if "12" in info.pix_fmt else 8))
    )
    info.color = "/".join(filter(None, [
        v.get("color_primaries"), v.get("color_transfer"), v.get("color_space"),
    ]))
    info.has_b_frames = to_int(v.get("has_b_frames"))
    info.start_time = to_float(v.get("start_time")) or 0.0

    if not info.duration:
        info.duration = to_float(v.get("duration")) or 0.0

    return info


# ================================================================ 关键帧
def _frames_by_packet(path: str, timeout: int) -> list[float] | None:
    """容器层扫描：不启动解码器，不占显存。失败返回 None 交给上层回退。"""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path]
    code, out, _ = run(cmd, desc="关键帧扫描(packet)", timeout=timeout)
    if code != 0 or not out.strip():
        return None

    kfs: list[float] = []
    seen = 0
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        seen += 1
        t = to_float(parts[0], None)  # type: ignore[arg-type]
        if t is None:
            continue
        if parts[1][:1] == "K":        # flags 形如 "K__" / "K_D"
            kfs.append(t)
    if not kfs:
        # 有包但没有任何 K 标记 = 这个封装没写关键帧信息
        log.debug("packet 扫描到 %d 个包但无 K 标记，回退 frame 层", seen)
        return None
    return sorted(kfs)


def _frames_by_decode(path: str, timeout: int) -> list[float]:
    """解码层扫描：慢一些，但只依赖解码器判断，不依赖容器标记。"""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
           "-of", "csv=p=0", path]
    code, out, _ = run(cmd, desc="关键帧扫描(frame)", timeout=timeout)
    if code != 0 or not out.strip():
        return []
    kfs: list[float] = []
    for line in out.splitlines():
        s = line.strip().rstrip(",")
        if not s:
            continue
        t = to_float(s, None)  # type: ignore[arg-type]
        if t is not None:
            kfs.append(t)
    return sorted(kfs)


def list_keyframes(path: str, timeout: int = 900,
                   method: str = "auto") -> list[float]:
    """返回视频流全部关键帧的 pts_time（秒，升序）。

    method: "auto"（默认，packet 优先、失败回退 frame）/ "packet" / "frame"
    ★ 返回容器原始 pts，不减去 start_time。因为 ffmpeg 的 -ss 同样以容器
      时间轴为准，两边保持同一基准才不会平白引入几十毫秒偏差。
    """
    if method == "frame":
        kfs = _frames_by_decode(path, timeout)
    elif method == "packet":
        kfs = _frames_by_packet(path, timeout) or []
    else:
        kfs = _frames_by_packet(path, timeout)
        if kfs is None:
            log.info("packet 层拿不到关键帧，改用 frame 层解码扫描（会慢一些）")
            kfs = _frames_by_decode(path, timeout)

    if not kfs:
        log.warning("未能获取任何关键帧，可能不是常规视频封装: %s", path)
        return []

    gaps = [b - a for a, b in zip(kfs, kfs[1:])]
    log.info("🔑 关键帧 %d 个 | 首 %.3fs | 末 %.3fs | 最大间隔 %.3fs",
             len(kfs), kfs[0], kfs[-1], max(gaps) if gaps else 0.0)
    return kfs


def pick_cutpoint(kfs: list[float], target: float,
                  side: str = "before") -> float | None:
    """把请求时间吸附到可用切点。side 的语义只有一种正确解读：

    side="after"  取 >= target 的第一个关键帧 —— 用于**头切**。
                  从这一帧起才有可解码画面，取更早的帧会得到坏帧。

    side="before" 取 <= target 的最后一个关键帧 —— 用于**尾切**。
                  在这一帧结束不会丢内容，代价是尾部多留一点。

    反过来用会出现：头切留残影、尾切吃掉正片末尾。这是物理约束。
    """
    if not kfs:
        return None
    if side == "after":
        cand = [k for k in kfs if k >= target - 1e-6]
        return cand[0] if cand else kfs[-1]
    if side == "before":
        cand = [k for k in kfs if k <= target + 1e-6]
        return cand[-1] if cand else kfs[0]
    return min(kfs, key=lambda k: abs(k - target))


# ================================================================ 兼容层
def probe_keyframes(video_path: str, method: str = "auto") -> list[float]:
    return list_keyframes(video_path, method=method)


def nearest_keyframe(keyframes: list[float], target: float,
                     mode: str = "after") -> float | None:
    return pick_cutpoint(keyframes, target, mode)


def probe_video_info(video_path: str) -> VideoInfo | None:
    try:
        m = probe_streams(video_path)
    except RuntimeError as e:
        log.error("%s", e)
        return None
    return VideoInfo(
        duration=m.duration, width=m.width, height=m.height,
        fps=m.fps, codec=m.v_codec, pix_fmt=m.pix_fmt, nb_frames=0,
    )


def probe_audio_info(audio_path: str) -> AudioInfo | None:
    try:
        m = probe_streams(audio_path, require_video=False)
    except RuntimeError as e:
        log.error("%s", e)
        return None
    if not m.a_codec:
        return None
    return AudioInfo(
        duration=m.duration, sample_rate=m.a_rate,
        bits=m.a_bits, channels=m.a_channels, codec=m.a_codec,
    )


def describe(video_path: str) -> dict:
    """一次性给出四份结果，CLI 与批处理统一调用。"""
    return {
        "media": probe_streams(video_path),
        "video": probe_video_info(video_path),
        "audio": probe_audio_info(video_path),
        "keyframes": list_keyframes(video_path),
    }


# ================================================================ 缓存
def _cache_path(cache_dir: str | Path, src: Path) -> Path:
    """缓存名必须绑定文件的 size + mtime，否则换了素材会读到旧数据。

    ★ 旧版写成 f"__.probe.json"，f-string 里没有任何变量，
      实际就是所有素材共用一个缓存文件 —— 第二个文件必然读到第一个的元数据。
      这里把 name/size/mtime 一起哈希，文件被覆盖后自动失效。
    """
    st = src.stat()
    key = f"{src.name}|{st.st_size}|{int(st.st_mtime)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return Path(cache_dir) / f"{src.stem[:40]}.{digest}.probe.json"


def load_or_probe(path: str, cache_dir: str = ".forge_cache",
                  refresh: bool = False) -> MediaProbe:
    """带磁盘缓存的探测：同一文件反复跑流水线时不重复读取。"""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    cache = _cache_path(cache_dir, src)
    if cache.is_file() and not refresh:
        try:
            return MediaProbe(**json.loads(cache.read_text("utf-8")))
        except (json.JSONDecodeError, TypeError, OSError):
            log.warning("缓存损坏，重新探测: %s", cache.name)

    info = probe_streams(path, require_video=False)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(info.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    except OSError as e:
        log.warning("缓存写入失败（不影响结果）: %s", e)
    return info


__all__ = [
    "MediaProbe", "VideoInfo", "AudioInfo",
    "probe_streams", "list_keyframes", "pick_cutpoint", "load_or_probe",
    "probe_video_info", "probe_audio_info", "probe_keyframes",
    "nearest_keyframe", "describe",
    "run", "to_float", "to_int", "parse_fps", "_f", "_to_float", "_to_int",
    "FFMPEG", "FFPROBE", "MKVMERGE",
]


# ================================================================ 自测
if __name__ == "__main__":
    import sys
    import time

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("用法: python core/probe.py <文件> [--refresh] [--frame]")
        sys.exit(2)

    target = sys.argv[1]
    t0 = time.perf_counter()

    mi = load_or_probe(target, refresh="--refresh" in sys.argv)
    print(json.dumps(mi.to_dict(), ensure_ascii=False, indent=2))
    print(f"🎬 {mi.width}x{mi.height} @ {mi.fps:.3f}fps | {mi.v_codec} | "
          f"{mi.pix_fmt} | {mi.bit_depth}bit | VFR={mi.is_vfr}")
    if mi.a_codec:
        print(f"🎵 {mi.a_codec} {mi.a_rate}Hz/{mi.a_bits}bit/{mi.a_channels}ch "
              f"| 音频轨 {mi.a_count} 条 | "
              f"{'🏅 Hi-Res' if mi.is_hires else '⚠️ 需重采样'}")

    method = "frame" if "--frame" in sys.argv else "auto"
    kfs = list_keyframes(target, method=method)
    if kfs:
        print(f"🔑 前 5: {[round(k, 3) for k in kfs[:5]]}")
        print(f"🔑 末 5: {[round(k, 3) for k in kfs[-5:]]}")
        gaps = [b - a for a, b in zip(kfs, kfs[1:])]
        if gaps:
            print(f"📏 最大间隔 {max(gaps):.3f}s ← 这就是无损切割的误差上限")
    print(f"⏱ {time.perf_counter() - t0:.2f}s")
