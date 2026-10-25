# 踩坑记录 / Known Pitfalls

> 按时间倒序。写给未来的自己，也写给愿意来挑刺的人。

---

## 2026-10-25 · `-ss` 的位置会决定输出时长

测试素材：`恋ひ恋ふ縁 (Game Size).mkv`
4K / 3840×2160 / HEVC / 60fps / 109.434s / FLAC 48kHz 24bit 2ch
关键帧只有 29 个，间隔 0.833s ~ 4.167s。

| 写法 | 实测输出 | 判定 |
|---|---|---|
| `-ss 4.167 -i SRC -t 94.166` | 98.416s | ✗ 多出 4.25s |
| `-i SRC -ss 4.167 -t 94.166` | 94.249s | ✓ |
| `-i SRC -ss 4.167 -to 98.333` | 94.249s | ✓ |
| `-ss 4.167 -i SRC -to 98.333` | 102.583s | ✗ 多出 8.4s |

**结论**：输入侧 seek 与输出侧 `-t` / `-to` 混用会打乱 MKV 时间基准。
默认走 `-i` 前置，`-ss` 放后面。`--fast` 仅作预览用。

---

## 2026-10-25 · 片尾永远切不干净

末个关键帧在 106.667s，而总长 109.434s —— 最后 2.767 秒没有任何 I 帧。
这意味着「切掉结尾黑屏」这个需求，在纯 `-c copy` 下**物理上做不到**。

应对：允许这一段单独重编码（只重编码最后几秒，画质损失可忽略），
或者干脆接受多留几秒，让平台自己裁。

---

## 2026-10-25 · Windows 控制台的 GBK 会炸掉 emoji

`print("📄 文件")` 在 GBK 终端直接 `UnicodeEncodeError` 崩掉。
修法：

```python
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

注意 `set PYTHONUTF8=1` 在 cmd 里会传成空值，反而触发
`Fatal Python error: preconfig_init_utf8_mode`。

---

## 2026-10-25 · FLAC 的位深字段有两个名字

`bits_per_sample` 对 FLAC 常常返回 0，真正有值的是 `bits_per_raw_sample`。
探测时必须两个都试：

```python
bits = _to_int(st.get("bits_per_raw_sample")) or _to_int(st.get("bits_per_sample"))
```

---

## 2026-10-25 · 大文件塞进 git 会让人后悔

4K 素材动辄 300MB+，一个不小心 `git add .` 就能把仓库养成怪物。
`.gitignore` 里已经把 `*.mkv` `*.mp4` `_out/` 全部挡住。
发布物走 release 附件，不进版本控制。

---

## 待确认的理论问题

### 「填充静音 + 不重编码」互斥
在音频前补 1390ms 静音 = 改动音频数据 = 必须重编码。
真正零重编码的路只有改时间戳：`mkvmerge --sync 0:1390`。
代价是部分播放器/平台不认这层偏移。

### 升采样不等于提升音质
44.1kHz → 48kHz 只是满足平台 Hi-Res 标签要求，不会让声音变好。
本工具会如实告知，不包装成「增强」。

---

## 2026-10-25 · 端到端跑通，以及它教给我的五件事

首次完整跑通 `probe → cut → audio → mux`：

```
源     恋ひ恋ふ縁 (Game Size).mkv  3840x2160 HEVC 60fps / FLAC 48kHz 24bit
请求   3.000s → 60.000s，+1130ms 音频延迟
落点   4.167s → 60.000s（55.833s）
成品   _out/e2e5.mkv  204.6MB | 1 视频 + 1 FLAC | 56.9s | 耗时 3.11s
```

### 1. `-ss` 和 `-t` 不能待在同一个 `-c copy` 命令里

`-i SRC -ss 10.042 -t 5` 在 120fps / GOP=10.042s 的素材上，
产出 **653 字节的空文件，退出码却是 0**，`-loglevel error` 下完全静默。

必须拆两段：

| 段 | 命令 | 作用 |
|---|---|---|
| 1 | `-i SRC -ss START -c copy` | 只切头，建立新基准 |
| 2 | `-i TMP -t DUR -c copy` | 在新基准上截时长 |

一段 = 空文件；两段 = 55.866s，一次通过。

### 2. `-map 0` 会把封面图当第二条视频轨塞进成品

源文件第三条流是 `mjpeg` 附封面（`attached_pic`）。
`-map 0` 会让它混进去，mkvmerge 之后变成两条视频轨。

固定 `-map 0:v:0 -an -sn -dn -map_chapters -1`。
`probe_streams` 里也要按 `disposition.attached_pic` 排除，
否则会读到封面图的帧率（90k tbr）。

### 3. FLAC 没有容器时长，别拿 `format=duration` 当判据

MKV 里 FLAC 音轨的 `duration` 是 `N/A`，`format=duration` 给的是
**容器的**时长（按最长轨算，109.434s），而音轨实际只有 108.339s。
差的 1.096s 会让校验永远失败。

正确做法：`stream=duration` → 不行就扫包取 `max(pts + duration)`。

### 4. `adelay` 之后必须 `asetpts=PTS-STARTPTS`

`adelay=1130:all=1` 会插入真静音样本，同时把 PTS 整体后移。
此时直接跟 `atrim=duration=55.833`，因为时间轴已偏移，
会多切掉 1.088s —— 成品 54.745s，比预期短一截。

正确链：`atrim(窗口)` → `asetpts` → `adelay` → `asetpts` → `atrim(窗口长)` → `asetpts`。
最后那步裁尾不能省，不裁成品会比画面长 1.13s。

### 5. 验证「真静音」不能用 max_volume

要用 `silencedetect` 量开头静音段长度，看够不够 1130ms。

用 `volumedetect` 取「delay + 200ms」窗口的 max_volume，
窗口尾部已含正常音频，永远读到 -0.2dB，合格成品被误判成失败。

### 6. `ffprobe` 没有 `-sseof`

`-sseof` 是 **ffmpeg** 的输入选项，`ffprobe` 直接报 `Option not found`。
从尾部读只能用 `-read_intervals` 给起点。

---

## 2026-10-25 · `mkvmerge` 的选项作用域

选项只作用于**它后面那个输入文件**：

```bash
# 错：--no-audio 属于「下一个文件」audio.flac，等于白写
mkvmerge -o out.mkv video.mp4 --no-audio audio.flac

# 对：选项在前，文件在后
mkvmerge -o out.mkv --no-audio video.mp4 --sync 0:0 audio.flac
```

不报错，不警告，原音轨照样进成品。只能靠 `verify_mux` 数轨道才发现。

退出码：`0` 成功 / `1` 有警告但可用 / `2` 失败。
`ok_codes=(0, 1)`，且 1 要打警告 + 走回读校验。

---

## 2026-10-25 · 延迟只能在一处生效

| 方案 | 实现位置 | 优点 | 代价 |
|---|---|---|---|
| A 物理延迟 | `audio.py` 的 `adelay` | 真静音样本，转码也丢不掉 | 整轨重编码 |
| B 容器偏移 | `mux.py` 的 `--sync` | 零耗时零损失，改数字重跑 3 秒 | 部分转码服务会丢偏移 |

`AudioSpec.already_delayed` 与 `MuxSpec.audio_delayed` 各有一条断言，
同时用会把延迟叠成 2260ms，构造时直接抛 `ValueError`。

---

## 2026-10-25 · 源文件可能是坏的，退出码不会告诉你

`魔女4k-Hires (4).mkv` 那条 FLAC，容器里 `DURATION: 00:00:00`，
整轨转出来只有 8KB / 1.13s，ffmpeg 退出码 0。

`audio_source_ok()` 现在先探 5 秒的包，读不到或总量 < 1KB 就拦下，
并明说「是源坏了，不是参数错了」。
