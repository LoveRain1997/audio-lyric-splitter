---
name: audio-lyric-splitter
description: "歌词感知、节拍对齐的 MP3 分段工具，用于把一整首歌切成 10–15 秒、绝不切断唱句、切点卡在气口/节拍上的 H3/多段视频素材，并输出每段段内节奏点(beats)与字位(lyric_events)的 manifest 和打包 zip。开始前必须先确认用户是否提供逐句歌词 SRT：有 SRT 直接切；没有歌词或只有音频时，先用 scripts/transcribe_lyrics.py 经 mediakit-cli ASR（唱歌 singing 模式、中/英文）识别出带时间戳的 SRT 草稿，人工校对后再切。触发场景：歌曲/音频分段、按歌词切割、不切歌词、节拍卡点、H3 多段、歌词时间轴 SRT、识别歌词、语音转字幕/ASR 生成歌词、生成 cuts_manifest.json、段内节奏点。"
---

# Audio Lyric Splitter

歌词感知的节拍对齐 MP3 分段：先拿到逐句歌词时间轴，再分析 BPM/节拍网格，用动态规划把整首歌切成每段 10–15 秒、**绝不切断唱句**、切点尽量落在气口并贴近节拍/小节的片段，输出段内节奏点与字位 manifest，可选打包 zip。

## 依赖

- Python 3 + `numpy` + `scipy`（仅分析脚本需要）；切割脚本只用标准库。
- `ffmpeg` / `ffprobe` 在 PATH（找不到时在脚本顶部 `FFMPEG_FALLBACK` 追加绝对路径）。
- 识别歌词还需 `mediakit-cli`（豆包环境内置，云端 ASR；本地音频由 CLI 自动上传）。

## 工作流

### 第 0 步：获取逐句歌词 SRT（必做，先问用户）

切割的“不切词”完全依赖逐句时间轴，开始前**先问用户能否提供歌词**，按情况处理：

1. **用户提供了逐句 SRT/字幕文件**（标准 SubRip，`HH:MM:SS,mmm --> HH:MM:SS,mmm`）→ 直接进入第 1 步。
2. **只有歌词文本、没有时间轴** → 时间轴仍需获取：走下面的 ASR 识别得到时间轴，再用用户给的原文逐句替换/校对文字。
3. **什么都没有（只有音频）** → 用 `scripts/transcribe_lyrics.py` 识别：

   ```
   python scripts/transcribe_lyrics.py --audio "song.mp3" --language zh --content-type singing
   # --language: zh(简体) | en(英语) | auto(默认自动探测)
   # --content-type: singing(默认唱歌) | speech(念白) | auto(自动)
   ```

   产出与音频同名的 `.srt`（草稿）和 `.asr.json`（含每句置信度），并在终端列出低置信句。

**ASR 草稿必须人工校对后才能切割（硬要求）。** 唱歌（尤其古风/戏曲/慢板/和声/强伴奏）普遍错字，间奏会误识别出“啊”等拟声，多个短句可能被合并成一条长句。校对三件事：对照原词逐句改字 → 删掉间奏里的误识别短句 → 按真实句读拆分长句、核对每句起止边界。未校对的草稿直接切，常出现“约束下无解”或仍然切词。

强伴奏识别过差时，先用 `mediakit-cli audio separate-voice` 分离出人声轨（读取 `byted-mediakit-audio` 的 separate-voice reference），再对人声轨跑识别。宁可请用户补歌词，也不要拿未校对的时间轴硬切。

### 第 1 步：分析 BPM 与节拍网格

```
python scripts/analyze_audio.py "song.mp3" --out analysis.json
```

输出 BPM 候选（含半速/加倍）、选定 BPM、首拍、beat/bar/phrase 间隔、低能量间奏区间。
慢板/古风常把正确速度报成“半速”：看候选里的半/倍速，用 `--bpm`（必要时加 `--first-beat`）锁定正确者。间奏一律以 SRT 的无唱句区间为准。

### 第 2 步：先看方案，确认不切词

```
python scripts/cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --plan
# BPM 不对：--bpm 60.75 [--first-beat 0.488]；也可用 --analysis analysis.json
```

`--plan` 只打印切点/段长/每段歌词/切词检查，不切文件。确认“切词 0 处、唱句完整”再继续。

### 第 3 步：实切 + manifest + 打包

```
python scripts/cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --title "歌名"
```

去掉 `--plan` 即实切；给 `--title` 额外生成 `<title>_H3分段_N段_不切歌词.zip`。切完自动用 ffprobe 独立自检（实测段长、求和、逐句完整、无切词），不通过以退出码 1 报错。

## 脚本

- `scripts/transcribe_lyrics.py`：无歌词时用 mediakit-cli ASR 识别，生成 SRT 草稿 + `.asr.json`，列低置信句；**草稿必须人工校对**。
- `scripts/analyze_audio.py`：ffmpeg 解码后做 onset 包络自相关测 BPM（半/倍速相位扫描防八度错）、估首拍、列低能量区间。
- `scripts/cut_by_lyrics.py`：内置标准 SRT 解析器（兼容 UTF-8 BOM/GBK、逗号毫秒、CRLF、`<i>`/`{\an8}` 标签、多行歌词、中英文）；全局动态规划选切点；写 manifest；ffprobe 自检；可选 zip。

## 切割规格（默认）

每段目标 12.5s、允许 10–15s（`--min/--max/--target` 可调）。合法切点不得落在任何唱句开区间内（两句首尾相接的精确交界允许切）；宽气口（gap≥0.15s）切点须距前后句边界留 0.06s 保护，防止削字头；候选集含 0、总长、安全拍点、零缝交界、气口 0.02s 密采；DP 代价为段长偏离目标的平方，并对贴近小节首拍/普通拍给偏好。**节拍对齐永远让位于“不切词”。**

## manifest（cuts_manifest.json）

顶层 `meta`（source/srt/bpm/beat_sec/bar_sec/phrase_sec/first_downbeat_sec/min/max/target/total_duration/segment_count/accent_legend/note）；每段 `segments[]` 含：

- `beats`：`{local, global, k, accent}`，accent 四级 phrase(k%8，8拍/2小节) / downbeat(k%4，小节) / onbeat(k%2) / offbeat；`local` 相对段头，供 H3 段内卡点。
- `lyric_events`：`{text, local_start, local_end, global_start, global_end}`，逐句字位；散板/自由节奏以字位为准。

## 失败处理

BPM 半速/加倍、“约束下无解”、零缝连唱、散板、ASR 识别质量差或 mediakit-cli 不可用等情况，见 `references/troubleshooting.md`。
