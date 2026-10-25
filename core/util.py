"""Auto-Forge-MKV / core/util.py

公共工具：进程调用、数值解析、时间格式化、工具定位、文件清理。

为什么单独抽出来：
    这些东西原本在 probe.py 里，结果 cut.py / audio.py / mux.py 全都
    反向 import probe，只为了拿一个 run()。依赖方向变成环，测试也难切断。
    现在 probe / cut / audio / mux 一律从 util 拿基础设施，probe 只负责探测。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

log = logging.getLogger("forge")


# ================================================================ 工具定位
def find_tool(name: str, extra_paths: tuple[str, ...] = ()) -> str:
    """先查 PATH，再查常见安装位置。找不到就原样返回，让调用处报错。"""
    hit = shutil.which(name)
    if hit:
        return hit
    for p in extra_paths:
        if p and Path(p).is_file():
            return p
    return name


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
MKVMERGE = find_tool("mkvmerge", (
    r"C:\Program Files\MKVToolNix\mkvmerge.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
))
MKVEXTRACT = find_tool("mkvextract", (
    r"C:\Program Files\MKVToolNix\mkvextract.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvextract.exe",
))


def init_tools() -> dict[str, str]:
    """在 CLI 起步时调一次，把发现的工具路径写进日志。"""
    found = {"ffmpeg": FFMPEG, "ffprobe": FFPROBE,
             "mkvmerge": MKVMERGE, "mkvextract": MKVEXTRACT}
    for k, v in found.items():
        if Path(v).is_file():
            log.debug("工具 %s → %s", k, v)
        else:
            log.warning("⚠️ 未找到 %s（名为 %s）", k, v)
    return found


def have(path: str) -> bool:
    """工具是否真的可用。"""
    return Path(path).is_file() or bool(shutil.which(path))


# ================================================================ 进程调用
def run(cmd: list[str], desc: str = "", timeout: int = 600,
        ok_codes: tuple[int, ...] = (0,)) -> tuple[int, str, str]:
    """执行外部命令，统一编码与异常处理。返回 (code, stdout, stderr)。"""
    log.debug("%s: %s", desc or "run", _pretty(cmd))
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        log.error("找不到可执行文件: %s", cmd[0])
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        log.error("%s 超时（%ss）", desc or cmd[0], timeout)
        return 124, "", "timeout"
    except OSError as e:
        return 126, "", str(e)

    if p.returncode not in ok_codes:
        log.debug("%s stderr: %s", desc or "run", (p.stderr or "").strip()[:600])
    return p.returncode, p.stdout or "", p.stderr or ""


def _pretty(cmd: list[str]) -> str:
    return " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)


def run_streamed(cmd: list[str], desc: str = "", timeout: int = 3600,
                 ok_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """把 stderr 逐行转进日志（长任务用它，便于看进度）。"""
    log.info("▶️ %s", desc or "run")
    log.debug("%s", _pretty(cmd))
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except OSError as e:
        return 126, str(e)

    tail: list[str] = []
    try:
        assert p.stdout is not None
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            if line.startswith(("frame=", "size=")) or "time=" in line:
                log.debug("%s", line)
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()  # 收尸：否则子进程句柄泄漏，Windows 上会卡住后续 ffmpeg 调用
        return 124, "timeout"

    code = p.returncode or 0
    if code not in ok_codes:
        log.error("❌ %s 退出码 %s", desc or "run", code)
    return code, "\n".join(tail)


# ================================================================ 数值解析
def to_float(v, default: float | None = 0.0) -> float | None:
    """安全转 float。NaN 视为无效；default=None 表示希望拿到「无效」信号。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    return x


def to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_fps(s) -> float:
    """'24000/1001' 或 '60' → 浮点帧率。0/0、N/A、空串一律 0.0。"""
    if not s:
        return 0.0
    try:
        return float(Fraction(str(s)))
    except (ZeroDivisionError, ValueError):
        return 0.0


def fmt_sec(sec: float, ms: bool = False) -> str:
    """秒 → ffmpeg 友好的定点字符串。

    ★ 绝不能用 str(float)：1e-07 会写成科学计数法，
      不同版本 ffmpeg 对它的解析行为不一致，属于静默踩坑。
    """
    if sec is None:
        sec = 0.0
    return f"{sec:.3f}" if ms else f"{sec:.6f}"


def fmt_dur(sec: float) -> str:
    """秒 → HH:MM:SS.mmm，给人看。

    ★ 先把整段时长折成千分位整数再拆分，不能对秒个别做 f"{s:06.3f}"：
      59.9999 秒会输出 "00:00:60.000"，7199.9999 秒会输出 "01:59:60.000"，
      多出来的那一位 60 秒既不合法，也会把日志里的时间轴读歪。
    """
    if sec is None or sec < 0:
        return "00:00:00.000"
    total_ms = int(round(sec * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ================================================================ 文件
def ensure_parent(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def cleanup(*paths: str | Path | None) -> None:
    """删掉 -y 建出来的半成品。失败不抛异常，只记一行警告。"""
    for p in paths:
        if not p:
            continue
        f = Path(p)
        if f.is_file():
            try:
                f.unlink()
                log.debug("已清理: %s", f)
            except OSError as e:
                log.warning("清理失败 %s: %s", f, e)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}TB"


def mask_secret(s: str | None) -> str:
    """日志里打密钥时只露头尾。"""
    if not s:
        return "(空)"
    return f"{s[:6]}…{s[-4:]}" if len(s) > 12 else "***"


def ensure_utf8_console() -> None:
    """让中文在 Windows 控制台里活下来。

    ★ 只把 Python 侧的编码改成 UTF-8 并不够：
      控制台自己的代码页还是 936，读 UTF-8 字节就会显示成
      「鉂?婧愭枃浠朵笉瀛樺湪」这种乱码。
      所以冻结成 exe 时要顺手把控制台代码页也切到 65001。
    ★ 输出被重定向到文件/管道时同理，否则写进去的是 GBK 字节，
      另一头按 UTF-8 读就炸。所以这一步要尽早做，别等 logging 初始化。
    """
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(level: int = logging.INFO, quiet: bool = False) -> None:
    """统一日志格式；先在 Windows 下把编码收拾干净，再挂 handler。"""
    ensure_utf8_console()
    logging.basicConfig(
        level=(logging.WARNING if quiet else level),
        format="%(levelname)s %(message)s",
    )


def plain_name(path: str) -> str:
    """Windows 下 os 层有时给 \\\\?\\ 前缀，打印时去掉。"""
    return str(path).replace("\\\\?\\", "")


__all__ = [
    "FFMPEG", "FFPROBE", "MKVMERGE", "MKVEXTRACT",
    "find_tool", "init_tools", "have",
    "ensure_utf8_console", "setup_logging",
    "run", "run_streamed",
    "to_float", "to_int", "parse_fps", "fmt_sec", "fmt_dur",
    "ensure_parent", "cleanup", "human_size", "mask_secret",
    "setup_logging", "plain_name",
]
