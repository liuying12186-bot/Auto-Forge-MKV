"""Auto-Forge-MKV / core/__init__.py

把四层暴露成统一的对外接口，方便 cli 与 tests 只 import 一次。
"""

from .probe import (
    AudioInfo,
    VideoInfo,
    describe,
    nearest_keyframe,
    probe_audio_info,
    probe_keyframes,
    probe_video_info,
    run,
)
from .cut import CutPlan, build_cut_cmd, execute, plan_cut

__all__ = [
    "AudioInfo", "VideoInfo", "describe", "nearest_keyframe",
    "probe_audio_info", "probe_keyframes", "probe_video_info", "run",
    "CutPlan", "build_cut_cmd", "execute", "plan_cut",
]

__version__ = "0.1.2"
