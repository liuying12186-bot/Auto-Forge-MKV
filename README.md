# Auto-Forge-MKV

> 一款为 4K / 120fps / Hi-Res 极致画质追求者打造的音视频自动化封装工具。

专治 Galgame OP/ED 二创时的三件烦心事：源带 UI 要切、外挂无损音轨差几毫秒、
剪辑软件一导出就重编码掉画质还丢 FLAC 真无损（痛失 B 站 Hi-Res 金标）。

## 状态

| 层 | 文件 | 状态 |
|---|---|---|
| util 基础设施 | `core/util.py` | ✅ 进程调用 / 数值解析 / 工具定位 |
| probe 探测 | `core/probe.py` | ✅ 实测（packet 扫描 0.60s vs frame 解码 1.84s，结果一致） |
| cut 切割 | `core/cut.py` | ✅ 实测（两段式，55.866s 首包 pts 0.033） |
| audio 音频 | `core/audio.py` | ✅ 实测（adelay 真静音 + 尾部裁回，静音段实测 1.130s） |
| mux 封装 | `core/mux.py` | ✅ 实测（1 视频 + 1 FLAC，轨道数回读校验） |
| cli 入口 | `cli/main.py` | ✅ 端到端跑通（`--dry-run` / `--keep`） |

首次端到端成品（2026/10/25）：

```
源     恋ひ恋ふ縁 (Game Size).mkv   3840x2160 HEVC 60fps / FLAC 48kHz 24bit
请求   3.000s → 60.000s，+1130ms 音频延迟
落点   4.167s → 60.000s（55.833s，头误差 +1.167s 受关键帧限制）
成品   1 视频 + 1 FLAC | 204.6MB | 总耗时 3.11s
```

## 快速开始

```bash
# 全流程 dry-run：只打印命令，不执行
python cli/main.py "source.mkv" --start 3 --end 60 --delay 1130 --dry-run

# 真跑
python cli/main.py "source.mkv" --start 3 --end 60 --delay 1130 --name "OP"

# 单层调试
python core/probe.py "source.mkv"
python core/cut.py  "source.mkv" --start 3 --end 25 --run
python core/audio.py "source.mkv" --delay 1130 --run
python core/mux.py  video.mp4 audio.flac --out out.mkv --run

# 测试
python -m unittest discover -s tests -v
```

## 核心设计取舍

### 1. 无损切割只能落在关键帧

`ffmpeg -c copy` 是无损的，但它只能在 I 帧起头。实测素材 109.4 秒里
只有 29 个关键帧，间隔 0.833s ~ 4.167s，这就是切割精度的天花板。
本工具不会偷偷切错，而是把「请求区间」与「实际落点」并排打出来。

### 2. `-ss` 和 `-t` 必须分成两条命令

2026/10/25 实测（120fps，GOP=10.042s）：

| 写法 | 输出 | 判定 |
|---|---|---|
| `-i SRC -ss 10.042 -t 5` | 653 字节空文件，退出码 0 | ❌ 完全静默 |
| `-ss 10.042 -i SRC -t 5` | 15.100s | ❌ 多出一个 GOP |
| 两段式 | 5.059s | ✅ |

所以固定两段：先切头建立新基准，再在新基准上截时长。
`tests/test_cut.py` 有一条断言专门盯着「同一条命令不许同时出现 `-ss` 和 `-t`」。

### 3. 延迟只能在一处生效

| 方案 | 实现位置 | 优点 | 代价 |
|---|---|---|---|
| A 物理延迟 | `audio.py` 的 `adelay` | 真静音样本，转码也丢不掉 | 整轨重编码 |
| B 容器偏移 | `mux.py` 的 `--sync` | 零耗时零损失 | 部分转码服务会丢偏移 |

默认走 A（`--audio-mode physical`）。两者同时用会把延迟叠成 2260ms，
所以 `AudioSpec` 和 `MuxSpec` 各有一条断言，构造时直接抛 `ValueError`。

### 4. 升采样不等于提升音质

44.1kHz 升到 48kHz 只是满足平台 Hi-Res 标签要求，不会让声音变好。
本工具会如实告知，不假装是「增强」。

## 依赖

- Python 3.10+
- FFmpeg / ffprobe（PATH 里能找到即可）
- MKVToolNix（`mux` 层需要；会自动探测 `C:\Program Files\MKVToolNix`）

## 目录

```
Auto-Forge-MKV/
├── cli/           命令行入口（单文件流水线）
├── core/          util / probe / cut / audio / mux
├── docs/          技术笔记与踩坑记录（pitfalls.md）
└── tests/         测试
```

## 许可

待定。
