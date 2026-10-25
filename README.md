# Auto-Forge-MKV

> 一款为 4K / 120fps / Hi-Res 极致画质追求者打造的音视频自动化封装工具。

专治 Galgame OP/ED 二创时的三件烦心事：源带 UI 要切、外挂无损音轨差几毫秒、
剪辑软件一导出就重编码掉画质还丢 FLAC 真无损（痛失 B 站 Hi-Res 金标）。

## 状态

| 层 | 文件 | 状态 |
|---|---|---|
| probe 探测 | `core/probe.py` | ✅ 已实测（4K HEVC 60fps，2.3s 扫完 109s 素材） |
| cut 切割 | `core/cut.py` | ✅ 已实测（`-i` 前置模式时长误差 0.08s） |
| audio 音频 | `core/audio.py` | ⏳ 待写（延迟 / 重采样 / FLAC 锁定） |
| mux 封装 | `core/mux.py` | ⏳ 待写（mkvmerge 合并、剔垃圾轨） |
| cli 入口 | `cli/main.py` | ✅ `probe` / `cut` 可用 |

## 快速开始

```bash
# 只看参数与关键帧分布，不动文件
python cli/main.py probe "source.mkv"

# 规划切割（dry-run，打印施工图与关键帧偏移）
python cli/main.py cut "source.mkv" --start 3 --end 100

# 真的切
python cli/main.py cut "source.mkv" --start 3 --end 100 --run
```

## 核心设计取舍

### 1. 无损切割只能落在关键帧
`ffmpeg -c copy` 是无损的，但它只能在 I 帧起头。实测素材 109.4 秒里
只有 29 个关键帧，间隔 0.833s ~ 4.167s，这就是切割精度的天花板。
本工具不会偷偷切错，而是把「请求区间」与「实际落点」并排打出来，
偏移多少明明白白告诉使用者。

### 2. `-ss` 必须放在 `-i` 之后
2026/10/25 实测（4K/60fps HEVC）：

| 写法 | 输出时长 | 结论 |
|---|---|---|
| `-ss 4.167 -i SRC -t 94.166` | 98.416s | ✗ 漂移 4.25s |
| `-i SRC -ss 4.167 -t 94.166` | 94.249s | ✓ |
| `-i SRC -ss 4.167 -to 98.333` | 94.249s | ✓ |
| `-ss 4.167 -i SRC -to 98.333` | 102.583s | ✗ |

输入侧 seek 与输出侧 `-t/-to` 混用会把 MKV 时间基准搞乱。
安全做法：`-i` 在前，`-ss` 在后，代价只是多解一小段。

### 3. 「填充静音」与「不重编码」互斥
想在音频前面补 1390ms 静音，本质是改音频数据，必然重编码。
两条路：
- **改时间戳**：`mkvmerge --sync 0:1390`，零重编码，但部分播放器/平台不认偏移。
- **PCM 中转**：解码成 PCM 再封回 FLAC，FLAC→FLAC 无损，代价几十秒。

### 4. 升采样不等于提升音质
44.1kHz 升到 48kHz 只是满足平台标签要求，不会让声音变好。
本工具会如实告知这一点，不假装是「增强」。

## 依赖

- Python 3.10+
- FFmpeg / ffprobe（已在 PATH）
- MKVToolNix（`mux` 层需要，尚未接线）

## 目录

```
Auto-Forge-MKV/
├── cli/           命令行入口
├── core/          probe / cut / audio / mux
├── docs/          技术笔记与踩坑记录
├── examples/      示例配置与批处理脚本
└── tests/         测试
```

## 许可

待定。
