"""Auto-Forge-MKV / core/batch.py

批处理：把一个目录里的视频逐个丢进同一条流水线。

设计约束
--------------------------------------------------------------------
1. 只挑视频扩展名，别把 .flac / .sub 之类误当源。
2. 默认不递归，加了 --recursive 才往子目录钻 —— 递归很容易把
   素材库里几十个片段一起点着了，不能当默认行为。
3. 排序必须稳定（按相对路径的字典序），否则两次跑出来的处理顺序不一样，
   排查问题时会怀疑人生。
4. 输出路径保留子目录结构，否则深层目录里同名的 op.mkv 会互相覆盖，
   而且不会报错，是最恶心的那种 bug。
"""

from __future__ import annotations

from pathlib import Path

# 常见容器。故意不含 .gif（动图）和 .m4a（纯音频）。
VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".mov", ".flv", ".webm",
    ".ts", ".m4v", ".avi", ".wmv", ".mpg", ".mpeg", ".rmvb",
})


def parse_exts(spec: str | None) -> frozenset[str]:
    """把 "mp4,mkv,.MOV" 这种手写列表规整成 {'.mp4', '.mkv', '.mov'}。"""
    if not spec:
        return VIDEO_EXTS
    out = set()
    for part in spec.replace(";", ",").split(","):
        p = part.strip().lower()
        if not p:
            continue
        out.add(p if p.startswith(".") else "." + p)
    return frozenset(out) or VIDEO_EXTS


def collect_sources(root: str | Path, exts: frozenset[str] = VIDEO_EXTS,
                    recursive: bool = False) -> list[Path]:
    """列出目录下待处理的视频，按相对路径字典序排好。"""
    base = Path(root)
    if not base.is_dir():
        return []
    it = base.rglob("*") if recursive else base.glob("*")
    hits = [p for p in it if p.is_file() and p.suffix.lower() in exts]
    return sorted(hits, key=lambda p: p.relative_to(base).as_posix().lower())


def out_path_for(src: Path, root: Path, out_dir: Path) -> Path:
    """成品路径：保留 src 相对 root 的子目录，避免同名文件互相覆盖。"""
    rel = src.relative_to(root)
    return out_dir / rel.parent / (src.stem + "_forge.mkv")


__all__ = ["VIDEO_EXTS", "parse_exts", "collect_sources", "out_path_for"]
