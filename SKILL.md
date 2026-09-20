---
name: audio-lyric-splitter
description: "歌词感知的音乐/音频分段切割：把整首歌按逐句歌词时间轴(SRT)切成每段默认10–15秒的 mp3，切点绝不切进唱词、优先落在句间气口并尽量对齐节拍，输出分段 mp3 + cuts_manifest.json（含每段段内节奏点 beats、唱句 lyric_events、BPM/首拍网格）+ 可选 zip，专为 MiniMax H3 / AI 舞蹈视频多段生成准备素材。当用户要求切割/切分/分段/分割歌曲或音频、按歌词切歌、不切歌词地分段、H3分段、把 mp3 切成约15秒片段、提取节拍网格或段内节奏点、beat-aligned mp3 split、cut/split song by lyrics or SRT 时使用。需要逐句歌词 SRT；只给 BPM 或能量无法保证不切词。"
---

# 音频歌词感知分段切割（Audio Lyric Splitter）

把整首歌切成每段 10–15 秒、**切点不切唱词**、尽量卡节拍的 mp3 片段，并产出带段内节奏点的 manifest，供 H3 / AI 视频逐段生成（多段音乐连续不重启）。

## 输入（硬要求）

- 音频：mp3/wav/m4a/flac 等 ffmpeg 支持格式。
- **逐句歌词 SRT**（标准 SubRip：序号行 / `00:00:00,000 --> 00:00:00,000` / 一行或多行歌词 / 空行分块）。
  - 逐句时间轴是「不切词」的硬约束。没有 SRT 时先取得或生成 SRT，**不要**只靠 BPM/能量切（必切词，已多次验证为死路）。

## 依赖

- Python3 + numpy + scipy（纯 numpy/scipy 分析，**不需要** librosa/pydub）。
- `ffmpeg`、`ffprobe` 在 PATH（Windows 可用 `winget install Gyan.FFmpeg`）。
- 检查：`python -c "import numpy,scipy"`、`ffmpeg -version`。
- Windows 路径含中文/空格/括号时整体加引号；跑中文输出前设 `$env:PYTHONUTF8=1`。

## 工作流（按序执行）

脚本在本 skill 的 `scripts/` 下。

**第 1 步：分析 BPM（建议先做）**
```
python scripts/analyze_audio.py "song.mp3" --out analysis.json
```
- 输出 `bpm / beat_sec / bar_sec / first_downbeat_sec / bpm_candidates / tested_half_double / low_energy_sec`。
- **慢板古风/散板**常出现半速与加倍两个都合理的值（如 60.75 与 121.5），自动值可能选错八度：结合听感定夺，必要时在下一步用 `--bpm` 锁定。

**第 2 步：先看方案，不切文件**
```
python scripts/cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --plan
```
- BPM/首拍来源优先级：`--bpm/--first-beat` > `--analysis analysis.json` > 自动分析。
- 输出必须同时满足：`全部∈[10,15]=True`、`进入唱词的切点=0`、`被切开的唱句: 无 —— N句全部完整`。
- BPM 不对：加 `--bpm 60.75`（必要时再加 `--first-beat 0.488`）后重看 plan。

**第 3 步：实切 + manifest + zip**
```
python scripts/cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --title "歌名"
```
- 生成 `segNN_MMmSSpSSs-MMmSSpSSs.mp3`（libmp3lame 192k）与 `cuts_manifest.json`，并在 `out` 同级打包 `<title>_H3分段_N段_不切歌词.zip`（或用 `--zip 路径`）。
- 脚本末尾用 ffprobe **独立回读自检**（实测段长、求和、逐句完整、无切词）；必须全部通过再交付。

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--min/--max/--target` | 10 / 15 / 12.5 | 段长上下限与趋优目标（秒） |
| `--bpm` / `--first-beat` | 自动 | 手动锁定 BPM / 首拍时间（慢板八度定夺、相位纠偏） |
| `--analysis` | 无 | 读 analyze_audio.py 的 JSON |
| `--guard` | 0.06 | 气口内切点距两侧唱词的保护秒数，防削下句字头 |
| `--gap-step` | 0.02 | 句间气口内候选点密采步长 |
| `--plan` | 关 | 只出方案与校验，不切割 |
| `--title` / `--zip` | 无 | 打包 zip（自动命名 / 指定路径） |

## 输出 cuts_manifest.json 结构

```
{
  "meta": { source, srt, bpm, beat_sec, bar_sec, phrase_sec, first_downbeat_sec,
            min_sec, max_sec, target_sec, total_duration, segment_count,
            accent_legend: {phrase, downbeat, onbeat, offbeat}, note },
  "segments": [
    { seg, start, end, length, file,
      "lyrics": [文本...],
      "lyric_events": [{text, local_start, local_end, global_start, global_end}],
      "beats": [{local, global, k, accent}] } ]
}
```
- `beats[].local` 相对段头秒数（H3 段内卡点直接用），`global` 为原曲时间，`k` 为全局拍序号。
- accent 四级，与 H3 的 8/4/2/1 拍编舞层级对齐：
  - `phrase`：k%8==0，每 8 拍/2 小节，乐句强拍；
  - `downbeat`：k%4==0，每 4 拍，小节强拍；
  - `onbeat`：k%2==0，正拍；`offbeat`：弱拍/反拍。
- **散板/自由句**：机械节拍网格只作身韵骨架，动作与卡点以 `lyric_events` 的字位为准。

## 硬规则（不可退化）

1. **不切词优先于节拍对齐**。切点合法当且仅当它不落在任何唱句开区间 `(start,end)` 内部；宁可切点不在拍上，也绝不切进唱词。
2. **零/近零缝连唱**（相邻句 gap < 0.15s）允许切在两句首尾相接的精确交界点；**宽气口**（gap ≥ 0.15s）切点必须落在 `[前句end+guard, 后句start−guard]`，贴后句 start 会削字头。
3. **间奏/纯器乐段以 SRT 空白为准**。满编曲间奏能量检测常失效，`analyze` 的 `low_energy_sec` 仅作辅助。
4. 段长用全局 DP 求最优、趋近 target，硬约束 `[min,max]`；候选集 = 拍点 ∪ 句交界 ∪ 气口内 0.02s 密采。
5. 交付前确认 ffprobe 自检全过。mp3 逐段重编码每段容器时长约 +26~31ms（帧补齐），切点仍在气口，属正常；多段求和通常与原时长差 ≤0.001s。

## 排错

无解、出现切词、BPM 八度错误、首拍相位偏、SRT 编码/多行、ffmpeg 缺失等，见 `references/troubleshooting.md`。
