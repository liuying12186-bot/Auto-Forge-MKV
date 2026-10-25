# Auto-Forge-MKV

> 一款为 4K / 120fps / Hi-Res 画质党打造的音视频自动化封装工具。

专治 Galgame OP/ED 与二创剪辑里的三件烦心事：源带 UI 要切、外挂无损音轨差几毫秒、
剪辑软件一导出就重编码掉画质还丢 FLAC 真无损（痛失 Hi-Res 金标）。

## 解决痛点

1. 游戏 / 录屏源带 UI，头尾需要切除。
2. 外挂音频源（如 CD 提取、无损母带）与视频存在毫秒级错位。
3. 常规剪辑软件导出必然重编码视频，且无法保留 FLAC 无损音轨。

## 核心特性

- [x] **两种延迟模式**：`adelay` 物理填静音 / `mkvmerge --sync` 容器偏移，二选一，禁止叠加。
- [x] **关键帧对齐的无损裁剪**：`-c copy` 秒级切割，视频零重编码。
- [x] **Hi-Res 条件校验**：按「采样率 ≥ 48kHz 且 位深 ≥ 24bit」判定，采样率与位深均可配置。
- [x] **一键封装**：`mkvmerge --no-audio` 剔除原音轨，合并无损音轨，成品回读轨道数校验。
- [x] **端到端实测**：4K HEVC 源 → 55.8s 成品 204.6MB，1 视频 + 1 FLAC，总耗时 3.11s。
- [ ] **批处理流水线**：遍历文件夹挂机跑（未实现）。
- [ ] **AI 简介生成**：接 DeepSeek 自动写平台简介（未实现）。

## 技术栈

- Python 3.10+
- FFmpeg / ffprobe（音视频处理核心）
- MKVToolNix（封装引擎）
- （计划）DeepSeek API：直接用 `openai` SDK，`base_url="https://api.deepseek.com"`、`model="deepseek-chat"`，无需自建 HTTP 层

## 快速开始

```bash
# 全流程 dry-run：只打印命令，不执行
python cli/main.py "source.mkv" --start 3 --end 60 --delay 1130 --dry-run

# 真跑（物理延迟，出成品）
python cli/main.py "source.mkv" --start 3 --end 60 --delay 1130 --name "OP"

# 保留中间产物 / 忽略 probe 缓存
python cli/main.py "source.mkv" --keep --refresh

# 保真：显式跟随源采样率，不要用默认 48k
python cli/main.py "source.mkv" --start 3 --end 60 --rate 192000

# 单层调试
python core/probe.py "source.mkv"
python core/cut.py   "source.mkv" --start 3 --end 25 --run
python core/audio.py "source.mkv" --delay 1130 --run
python core/mux.py   video.mp4 audio.flac --out out.mkv --run

# 测试
python -m unittest discover -s tests -v
```

## 三个必须说清楚的限制

### 1. 「填静音」与「不重编码」不能同时成立

FLAC 是压缩格式，要在流内部插入静音样本，就必须 **解码 → 填静音 → 再编码**。
这是无损重编码：音质零损失，但文件被完整重写，也不再是「零成本」。

| 方案 | 实现位置 | 优点 | 代价 |
|---|---|---|---|
| A 物理延迟 | `core/audio.py` 的 `adelay` | 静音真的在样本里，任何播放器 / 转码服务都丢不掉 | 整轨重编码 |
| B 容器偏移 | `core/mux.py` 的 `--sync` | 零耗时零损失，改个数字重跑约 3 秒 | 容器内无真实静音，部分平台转码会丢掉偏移 |

所以正确用法是：**先用 B 快速试错找准毫秒值，再用 A 出成品**（B 站转码有可能忽略容器延迟）。
两者同时用会把延迟叠成两倍，`AudioSpec` 与 `MuxSpec` 各有一条断言，构造时直接抛 `ValueError`。

真正「不重编码」的情形只有一种：源音轨本身已满足目标规格时 `needs_convert()` 返回 False，直接 `-c:a copy`。

### 2. 无损裁剪的精度由关键帧决定

`ffmpeg -c copy` 的切点只能落在关键帧（I 帧）上，误差上限等于 GOP 长度。
实测素材 109.4 秒只有 29 个关键帧，间隔 0.833s ~ 4.167s —— 这就是精度的天花板。

本工具不会偷偷切错，而是把「请求区间」和「实际落点」并排列出来：

```
请求   3.000s → 60.000s，+1130ms 音频延迟
落点   4.167s → 60.000s（55.833s，头误差 +1.167s，受关键帧限制）
```

片尾更麻烦：末个关键帧在 106.667s，而总长 109.434s，最后 2.767 秒没有任何 I 帧，
纯 `-c copy` **物理上切不干净**。要帧级精确，只能对那一小段单独重编码。

### 3. 采样率不要固定 48kHz

192kHz → 48kHz 会把 24kHz 以上的频率直接丢掉，是实打实的损失，不是「规格统一」。
平台 Hi-Res 也支持 96k / 192k，没有理由先自废一刀。

`--rate` / `--bits` 均可配置，但**默认值是 48000 / 24**。
保真优先请显式传 `--rate <源采样率>`（后续计划改为「默认跟随源、下限 48k」）。

另有一个静默坑：FLAC 的位深在 ffmpeg 侧必须写成 `s32`，容器里才会记成 24bit；
写成 `-sample_fmt s16` 会静默降成 16bit，金标当场消失且不报错。

## 其他已知坑

完整记录见 [`docs/pitfalls.md`](docs/pitfalls.md)，这里只列最贵的几条：

- `-ss` 与 `-t` 不能放在同一个 `-c copy` 命令里：会产出 653 字节空文件，**退出码却是 0**。固定拆两段。
- `mkvmerge` 的选项只作用于**它后面那个输入文件**，`--no-audio` 必须写在视频文件之前，否则原音轨照样进成品，不报错。
- `-map 0` 会把封面图（`attached_pic`）当第二条视频轨塞进成品，必须显式 `-map 0:v:0`。
- MKV 里的 FLAC 没有可靠 `duration`，校验时长要扫包取 `max(pts + duration)`。
- `adelay` 之后必须 `asetpts=PTS-STARTPTS`，否则后续 `atrim` 会多切掉一截。
- 验证「真静音」要用 `silencedetect` 量长度，不能用 `volumedetect` 的 `max_volume`。

## 依赖

- Python 3.10+
- FFmpeg / ffprobe（PATH 可见即可）
- MKVToolNix（`mux` 层需要，会自动探测 `C:\Program Files\MKVToolNix`）

## 目录

```
Auto-Forge-MKV/
├── cli/           命令行入口（单文件流水线）
├── core/          util / probe / cut / audio / mux
├── docs/          技术笔记与踩坑记录（pitfalls.md）
└── tests/         测试
```

## 开发者故事

本项目由一位被底层时间戳逼疯、为了完美 4K 120fps 追番体验而爆肝的开发者发起。

## 许可

待定。
