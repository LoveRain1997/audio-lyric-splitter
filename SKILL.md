---
name: audio-lyric-splitter
description: "歌词感知、节拍对齐的 MP3 分段工具，用于把一整首歌切成 10–15 秒、绝不切断唱句、切点卡在气口/节拍上的 H3/多段视频素材，并输出每段段内节奏点(beats)与字位(lyric_events)的 manifest 和打包 zip。开始前必须先确认用户是否提供逐句歌词 SRT：有 SRT 直接切；有歌词文本但无时间轴（含日语/韩语/俄语等非中英语种，或需要高精度）时，默认用 scripts/align_lyrics_local.py 本地管线（分离人声→faster-whisper→wav2vec2 CTC 对原文强制对齐→结尾齐唱重复取首遍→能量校验）直接产出 LRC+SRT 再切；什么都没有或中/英文歌可用 scripts/transcribe_lyrics.py 经 mediakit-cli 云端 ASR（唱歌 singing、中/英文）出草稿，人工校对后再切。需要真实鼓点/编曲事件（而非等间隔网格）时，用 scripts/detect_drum_events.py 经 demucs 鼓轨分离 + librosa onset + 频谱聚类反推 kick/snare/hihat/other 事件并标注置信度，再用 scripts/merge_drum_manifest.py 并入 manifest。触发场景：歌曲/音频分段、按歌词切割、不切歌词、节拍卡点、H3 多段、歌词时间轴 SRT/LRC、识别歌词、日语歌词对齐、语音转字幕/ASR、CTC 强制对齐、生成 cuts_manifest.json、段内节奏点、鼓点反推、鼓组转录、onset 起音检测、Kick/Snare/HiHat 时间戳、编曲事件表、drum_events。"
---

# Audio Lyric Splitter

歌词感知的节拍对齐 MP3 分段：先拿到逐句歌词时间轴，再分析 BPM/节拍网格，用动态规划把整首歌切成每段 10–15 秒、**绝不切断唱句**、切点尽量落在气口并贴近节拍/小节的片段，输出段内节奏点与字位 manifest，可选打包 zip。

## 依赖

- Python 3 + `numpy` + `scipy`（仅分析脚本需要）；切割脚本只用标准库。
- `ffmpeg` / `ffprobe` 在 PATH（找不到时在脚本顶部 `FFMPEG_FALLBACK` 追加绝对路径）。
- 云端识别（可选）：`mediakit-cli`（豆包环境内置，云端 ASR；本地音频由 CLI 自动上传），**仅支持中文（cmn-Hans-CN）/英文（eng-US）**。
- 本地对齐（`align_lyrics_local.py`，无歌词/无时间轴默认路径）：`torch`+`torchaudio`（CPU 版即可）、`transformers`、`faster-whisper`、`fugashi`+`unidic-lite`（日语注音）、`numpy`。首次运行自动下载 wav2vec2 CTC 模型；国内设 `HF_ENDPOINT=https://hf-mirror.com`。安装踩坑（torch CPU 国内源、PyAV 兼容等）见 `references/troubleshooting.md` 第 12 节。
- 鼓点反推（`detect_drum_events.py`，可选）：`demucs`（分轨取鼓轨，依赖 torch）、`librosa`（onset 检测，依赖 `numba`）、`scikit-learn`（频谱聚类）、`soundfile`。demucs 首次运行自动下载 htdemucs 模型。安装与网络踩坑见 `references/troubleshooting.md` 第 13 节。

## 工作流

### 第 0 步：获取逐句歌词时间轴（必做，先问用户）

切割的“不切词”完全依赖逐句时间轴，开始前**先问用户能否提供歌词**，按情况处理：

1. **用户提供了逐句 SRT/字幕文件**（标准 SubRip，`HH:MM:SS,mmm --> HH:MM:SS,mmm`）→ 直接进入第 1 步。
2. **有歌词文本、没有时间轴（推荐路径，尤其中文以外语种）** → 用 `scripts/align_lyrics_local.py` **本地管线**：它不依赖“识别出歌词”，而是**拿用户给的原文做 CTC 强制对齐**，直接产出 `.lrc` + `.srt` + `.align.json`。日语/韩语/俄语等云端 ASR 不支持的语种，以及齐唱/长拖音/强伴奏场景，走这条；中文、英文也能用。详见下方“第 0 步补充”。
3. **什么都没有（只有音频）且是中/英文歌** → 可用 `scripts/transcribe_lyrics.py` 云端 ASR 出草稿：

   ```
   python scripts/transcribe_lyrics.py --audio "song.mp3" --language zh --content-type singing
   # --language: zh(简体) | en(英语) | auto(默认自动探测)
   # --content-type: singing(默认唱歌) | speech(念白) | auto(自动)
   ```

   产出与音频同名的 `.srt`（草稿）和 `.asr.json`（含每句置信度），并在终端列出低置信句。**云端 ASR 不支持日语等语种**：`auto` 会把日语音节误识别成英文/罗马音，草稿不可用，必须改走第 2 条本地对齐。

**ASR 草稿（第 3 条）必须人工校对后才能切割（硬要求）。** 唱歌（尤其古风/戏曲/慢板/和声/强伴奏）普遍错字，间奏会误识别出“啊”等拟声，多个短句可能被合并成一条长句。校对三件事：对照原词逐句改字 → 删掉间奏里的误识别短句 → 按真实句读拆分长句、核对每句起止边界。未校对的草稿直接切，常出现“约束下无解”或仍然切词。

本地对齐（第 2 条）产物也应快速过一遍 `.align.json` 与终端的能量校验：主歌通常逐句准确；结尾齐唱/长过门已由脚本自动处理，但仍建议抽查首句、尾句与大合唱段。

### 第 0 步补充：本地多语种对齐（align_lyrics_local.py，无时间轴默认）

管线（均在本地，不上传）：

1. **人声分离（决定性）**：默认调 `mediakit-cli audio separate-voice` 取人声轨（对原始混音直接跑 ASR/对齐会大量漏句）；再用 ffmpeg 转 16k mono wav。已有干净人声 wav 用 `--voice` 跳过分离；纯人声/伴奏很轻可 `--no-separate`。
2. **faster-whisper 锚点**：`large-v3` 取词级时间/确认语种（可 `--no-whisper` 跳过，语种显式指定且时间紧时）。
3. **wav2vec2 CTC 对原文强制对齐**：模型按语种选（日语 `jonatasgrosman/wav2vec2-large-xlsr-53-japanese`、中文 `…-chinese-zh-cn`、英文 `…-english`），其他字符型 wav2vec2 用 `--align-model` 指定。日语用 fugashi+unidic-lite 取读音、片假名转平假名，少量英文按日式假名映射（`--en-kana "summer=さまー,days=でいず"`）。
4. **结尾齐唱重复取首遍**：结尾大合唱常把最后副歌尾句再唱一遍（音频两遍、文本一遍），全曲 CTC 会把单遍文本摊到两遍、越往后越慢。脚本自动在尾窗定位“首遍”（Whisper 行首锚点 + CTC 门控 + 首遍后持续人声能量兜底），只对齐首遍。可 `--no-tail-fix` 关闭、`--tail-lines` 调整重复句数。
5. **能量校验**：逐行检查起点是否落在有声区，落在低能量区会打印警告。

用法：

```
# 歌词准备成 txt（每行一句；独占一行的 [Verse]/[Chorus]/[Mikoto] 等段落标记会被自动剔除，
# 行内 [xxx] 标记也会被去除；一行若含两个短句需先手工拆成两行）
export HF_ENDPOINT=https://hf-mirror.com   # 国内首次下载模型时
python scripts/align_lyrics_local.py --audio "song.mp3" --lyrics "lyrics.txt" \
    --language ja --title "歌名"
# 已有分离好的人声 wav：--voice voice_16k.wav（跳过分离）
# 输出与音频同名（或 --out 指定 basename）的 .lrc / .srt / .align.json
```

歌词输入也支持 JSON（`[[text, section], ...]` 或 `[{"text":..., "sec":...}, ...]`）。拿到 `.srt` 后进入第 1–3 步正常切割。

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

### 第 4 步（可选）：鼓点/起音事件反推

manifest 默认的 `beats` 是按 BPM 推算的**等间隔理论网格**，不是真实鼓点，也不区分鼓件。需要真实事件时，对音频做数据驱动反推（**禁止凭网格编造 Kick/Snare 时间**）：

1. **demucs 分轨取鼓轨**（关键，去除人声与旋律乐器）：

   ```
   python -m demucs --two-stems drums -o workdir "song.mp3"
   # 产物 workdir/htdemucs/song/drums.wav
   ```

2. **onset 检测 + 频谱聚类**：

   ```
   python scripts/detect_drum_events.py --drum workdir/htdemucs/song/drums.wav \
       --out drum_events.json
   # 内部：librosa onset_detect -> 节拍网格门限 -> 频谱特征 KMeans 4 类
   # 输出 kick/snare/hihat/other，每事件带 confidence/strength/beat_index/grid_offset
   ```

3. **并入 manifest**：

   ```
   python scripts/merge_drum_manifest.py   # 路径在脚本内按实际位置配置
   ```

   产出含 `drum_events`（全局）、`drum_detection`（方法元数据）及每段段内 `drum_events`（带 `local`）的完整 JSON。

**边界与诚实要求**：这是启发式反推、非逐音真值；`other` 是不满足 kick/snare/hihat 典型频谱的打击（可能含 tom/打击乐/弱奏/分离残留），不强行归类；弱音可能漏检。检测到的实际 tempo 若与 `analyze_audio` 结果不一致，以检测为准并在 `drum_detection` 注明，不得掩盖。

## 脚本

- `scripts/align_lyrics_local.py`：**无时间轴时的默认入口**。人声分离→faster-whisper 锚点→wav2vec2 CTC 对给定原文强制对齐→结尾齐唱重复取首遍→能量校验，输出 LRC + SRT + `.align.json`。支持日/中/英（内置模型）及任意字符型 wav2vec2 语种（`--align-model`）；不依赖“识别出歌词”，对原文对齐，因此齐唱、强伴奏、长拖音下主歌仍逐句准。
- `scripts/transcribe_lyrics.py`：中/英文歌、什么歌词都没有时，用 mediakit-cli 云端 ASR 识别，生成 SRT 草稿 + `.asr.json`，列低置信句；**草稿必须人工校对**；不支持日语等非中英语种。
- `scripts/analyze_audio.py`：ffmpeg 解码后做 onset 包络自相关测 BPM（半/倍速相位扫描防八度错）、估首拍、列低能量区间。
- `scripts/cut_by_lyrics.py`：内置标准 SRT 解析器（兼容 UTF-8 BOM/GBK、逗号毫秒、CRLF、`<i>`/`{\an8}` 标签、多行歌词、中英文，日文同样适用）；全局动态规划选切点；写 manifest；ffprobe 自检；可选 zip。
- `scripts/detect_drum_events.py`：在 demucs 鼓轨上做 librosa onset 峰值检测、节拍网格门限与频谱特征 KMeans(4) 聚类，输出 kick/snare/hihat/other 事件及置信度；全部基于真实音频、可复现，不编造。
- `scripts/merge_drum_manifest.py`：把鼓事件并入 cuts_manifest，生成全局与段内（`local`）视图，并做归属一致性校验。

## 切割规格（默认）

每段目标 12.5s、允许 10–15s（`--min/--max/--target` 可调）。合法切点不得落在任何唱句开区间内（两句首尾相接的精确交界允许切）；宽气口（gap≥0.15s）切点须距前后句边界留 0.06s 保护，防止削字头；候选集含 0、总长、安全拍点、零缝交界、气口 0.02s 密采；DP 代价为段长偏离目标的平方，并对贴近小节首拍/普通拍给偏好。**节拍对齐永远让位于“不切词”。**

## manifest（cuts_manifest.json）

顶层 `meta`（source/srt/bpm/beat_sec/bar_sec/phrase_sec/first_downbeat_sec/min/max/target/total_duration/segment_count/accent_legend/note）；每段 `segments[]` 含：

- `beats`：`{local, global, k, accent}`，accent 四级 phrase(k%8，8拍/2小节) / downbeat(k%4，小节) / onbeat(k%2) / offbeat；`local` 相对段头，供 H3 段内卡点。**注意 beats 是等间隔理论网格，不是真实鼓点。**
- `lyric_events`：`{text, local_start, local_end, global_start, global_end}`，逐句字位；散板/自由节奏以字位为准。
- `drum_events`（可选，经第 4 步反推）：全局事件为 `{time, type, confidence, strength, beat_index, grid_offset}`；每段段内视图另含 `local`（相对段头）。`type` 为 kick/snare/hihat/other；`grid_offset` 正=拖拍、负=抢拍。顶层 `drum_detection` 记录方法、参数、置信度模型与局限。

## 失败处理

BPM 半速/加倍、“约束下无解”、零缝连唱、散板、云端 ASR 识别质量差或 mediakit-cli 不可用等情况，见 `references/troubleshooting.md`。本地对齐的依赖安装（torch CPU 国内源）、PyAV 兼容、尾段齐唱重复、长过门/长拖音、其他语种模型等，见同文件第 12 节「本地多语种对齐（align_lyrics_local.py）」。
