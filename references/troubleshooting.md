# 排错与算法参考（audio-lyric-splitter）

## 目录
1. [约束下无解（RuntimeError）](#1-约束下无解runtimeerror)
2. [仍有唱句被切开 / 切点进唱词](#2-仍有唱句被切开--切点进唱词)
3. [BPM 选错八度（半速/加倍）](#3-bpm-选错八度半速加倍)
4. [首拍相位偏移](#4-首拍相位偏移)
5. [间奏检测不到 / low_energy 为空](#5-间奏检测不到-low_energy-为空)
6. [SRT 解析问题（编码/多行/标签）](#6-srt-解析问题编码多行标签)
7. [ffmpeg / ffprobe 缺失](#7-ffmpeg--ffprobe-缺失)
8. [段长与帧补齐口径](#8-段长与帧补齐口径)
9. [算法简述与已验证死路（勿退化）](#9-算法简述与已验证死路勿退化)
10. [非默认场景](#10-非默认场景)
11. [歌词识别（ASR）：没有歌词 SRT 时](#11-歌词识别asr没有歌词-srt-时)
12. [本地多语种对齐（align_lyrics_local.py）](#12-本地多语种对齐align_lyrics_localpy)

---

## 1. 约束下无解（RuntimeError）

`约束下无解：可适当放宽 --min/--max，或检查 SRT 时间轴。`

含义：在「切点不进唱词 + 每段 ∈[min,max]」下找不到一条完整切分。常见原因与处理：

- **存在长句/连续演唱区长度 > max**：某段唱句（或零缝相连的多句合并区）本身超过 15s，中间没有合法气口。
  - 先看 plan 输出里 `合并演唱区 N 段`，定位超长区。
  - 该区内部若有极窄气口（< 0.15s），默认不当作宽气口；可适度下调 `--guard`（如 0.03）或对该处放宽，但要重看 plan 确认不削字头。
  - 或放宽该曲上限 `--max 16/17`（H3 单段上限一般 15s，超限需告知用户、自行评估模型时长）。
- **min 过大**：歌曲开头/结尾或间奏处不足 min 又必须成段，可下调 `--min`（如 9）。
- **SRT 时间轴错误**：句末时间 > 句首、时间错位会制造非法区。用文本编辑器核对 SRT。

排查命令：先 `--plan --max 20 --min 5` 看是否只是边界太紧；再决定是放宽参数还是修 SRT。

## 2. 仍有唱句被切开 / 切点进唱词

plan 末尾的两行是硬验收：
- `进入唱词的切点=N` 必须为 0；
- `被切开的唱句: 无 —— N句全部完整...` 必须为「无」。

若不为 0/无：
- 优先怀疑 SRT 时间与实际演唱不齐（字幕偏早/偏晚）。逐句听辨校正 SRT 后重切，不要靠放宽算法硬切。
- 切点贴在某句 start 上但听感削了字头：这是气口太窄、guard 不足，加大 `--guard`（如 0.08–0.12）。
- 实切后 ffprobe 自检会再次列出「被切开的唱句」「切点进入唱词内部」，有问题进程以退出码 1 结束，不得交付。

## 3. BPM 选错八度（半速/加倍）

自相关在 BPM 与其 2 倍/一半处常同样强。脚本已对主峰及半速/倍速做相位扫描自动选，但**慢板、散板、长音为主的古风艺术歌曲**仍可能选错。

- 看 `analyze` 输出的 `bpm_candidates` 与 `tested_half_double`：若同时出现约 60 与 121 两个值，需要人耳定夺。
- 判定原则：跟着能听到的「击拍/指挥拍」数，通常是较慢那个；慢板选半速后，标准 downbeat（k%4）即慢板小节强拍。
  - 例：《鹧鸪天·桂》候选 60.75 与 121.5，成片网格按 121.5 给更密的身韵点（beat≈0.49s），downbeat≈1.98s、phrase(k%8)≈3.95s 对应慢板小节；两种取法都可用，关键是**全曲统一**并在 manifest 中自洽。
- 锁定：`--bpm 60.75`（必要时配 `--first-beat`）。
- 流行/电子/舞曲一般自动值即正确，无需干预。

## 4. 首拍相位偏移

节拍周期（BPM）对但整列拍点平移了半个/一个拍：切点会系统性偏离强拍。

- 用 `analyze` 的 `first_downbeat_sec`，对照波形/第一声鼓点核对。
- 手动指定：`--first-beat 0.488`（秒，原曲时间）。
- 首拍只影响「节拍对齐偏好」与 manifest 的 beats 网格，**不影响不切词合法性**（合法性只由 SRT 决定）。

## 5. 间奏检测不到 / low_energy 为空

`low_energy_sec` 用每秒 RMS < 0.45×中位数、连续≥1s 判定。**满编曲间奏**（鼓/弦乐持续但无人声）能量并不低，会返回空——这是预期，不是故障。

- 间奏/纯器乐位置一律以 **SRT 的空白段**为准：相邻唱句之间的大段空白就是间奏，DP 会在其中按气口规则自由选点并对齐节拍。
- 低能量检测仅对「真·安静段落」（留白、尾奏衰减）有参考价值。

## 6. SRT 解析问题（编码/多行/标签）

解析器已内置：UTF-8 BOM（utf-8-sig）→ utf-8 → gbk → gb18030 回退；毫秒逗号或点；CRLF；去除 `<i></i>` 等 HTML 标签与 `{\an8}` 等 ASS 标记；中文多行无空格拼接、西文多行空格连接。

仍异常时：
- 确认时间行严格形如 `00:00:18,850 --> 00:00:25,950`（箭头两侧空格、冒号、逗号/点毫秒）。
- 序号行可有可无；一句歌词可跨多行；块之间用空行分隔。
- 若歌词含 `-->` 等罕见字符导致误判，清理该字符。
- 想确认解析结果，可临时 `python -c "import sys;sys.path.insert(0,r'scripts');from cut_by_lyrics import parse_srt;print(parse_srt(r'xx.srt'))"` 查看句数与时间。

## 7. ffmpeg / ffprobe 缺失

- 脚本经 `shutil.which` 查找；Windows 另兜底常见 WinGet 链接路径。
- 缺失时安装：`winget install Gyan.FFmpeg`，安装后确认 `ffmpeg -version`、`ffprobe -version` 可用（重开终端刷新 PATH）。
- numpy/scipy 缺失：`pip install numpy scipy`。无需 librosa、pydub。

## 8. 段长与帧补齐口径

- 切点按采样/秒级精确落在句缝或句交界；mp3 逐段重编码时每段**容器时长约 +26~31ms**（MP3 帧补齐/编码器延迟），ffprobe 实测段长可能比请求值略大，属正常。
- 校验容差已含 ±0.15s；多段实测求和通常与原曲差 ≤0.001s（多首实测精确相等）。
- 文件名 `segNN_MMmSSpSSs-MMmSSpSSs.mp3`：`m` 分隔分秒，`p` 代替小数点（避免文件系统/URL 问题）。

## 9. 算法简述与已验证死路（勿退化）

合法切点：`legal(t) = 不存在任何唱句使 start < t < end`（开区间）。这允许切在两句首尾相接的精确交界点（前句 end == 后句 start）。

候选切点集合：`{0, 总时长}` ∪ 通过安全过滤的每拍点 ∪ 零/近零缝句交界（相邻句 gap<0.15s 的前句 end）∪ 各句间气口与首尾留白内 0.02s 密采点。

安全过滤 `safe(t)`：
- 必须 legal；
- 紧邻零缝交界（<0.02s）放行；
- 落在宽气口 [a,b]（b−a≥0.15s）时要求 `a+guard ≤ t ≤ b−guard`；开头留白要求 `t ≤ b−guard`，结尾留白要求 `t ≥ a+guard`；窄气口（非交界）不放行。

动态规划：`cost = Σ(段长−target)² − beat_pref(右端点)`，段长硬约束 `[min−ε, max+ε]`（ε=0.08）；`beat_pref` 对贴近 downbeat(k%4) 的右端给 1.5、贴近普通拍给 0.6。全局取最优，使段长尽量均匀且右端尽量卡拍。

**已验证、不可回退的结论：**
1. 只靠 BPM + 能量/静音检测切分 → 大量切词。逐句 SRT 是不可替代的硬约束。
2. 只对齐 downbeat + 气口中点 → 临界情况下无解；必须在气口内 0.02s 密采。
3. 零间隙连续演唱下，把演唱区当闭区间全禁 → 无解；必须用**开区间**并放行精确句交界。
4. 宽气口里切点贴后句 start → 削字头；必须强制双侧 guard（默认 0.06s）。
5. 「节拍对齐」永远让位于「不切词」：二者冲突时选不切词。

## 10. 非默认场景

- 改段长规格：`--min 8 --max 12 --target 10` 等；H3 单段常用 10–15s。
- 只要分析不切割：单独运行 `analyze_audio.py`。
- 复用分析结果、避免重复计算：`analyze_audio.py --out analysis.json` 后 `cut_by_lyrics.py --analysis analysis.json ...`。
- 其他语言歌曲：SRT 解析与语言无关，中文/英文/日文同样适用（已验证中文慢板与英文快歌）。
- 切割结果用于 H3 多段时：每段音频对应原曲时间区间，多段提示词中音乐须声明 CONTINUES、不重启；段内卡点用 beats 的 `local` 时间与 accent 层级。

## 11. 歌词识别（ASR）：没有歌词 SRT 时

入口：`scripts/transcribe_lyrics.py`，内部调用 `mediakit-cli video asr-subtitles`（云端异步；本地音频由 CLI 自动上传；`--content-type singing` 唱歌模式、`--language zh/en/auto`、可开置信度），轮询终态后写标准 SRT 草稿与 `.asr.json`。

**质量预期（已实测，务必管理预期）：**
- 慢板古风艺术歌曲（《鹧鸪天·桂》75s）平均置信度约 **0.46**，12 句中 10 句 <0.5；文字大量错误（如“暗淡轻黄体性柔”识别成“安得清欢啼心忧”），间奏误识别出“啊/啊啊啊”，多个短句被合并成约 10s 的长句。
- 咬字清晰、人声突出、节奏规整的流行歌效果明显更好；但**唱歌 ASR 普遍不如念白**，戏曲/戏腔/和声/强伴奏最差。

**因此草稿必须人工校对（硬要求），三步：**
1. 对照原词逐句改错字（用户有歌词文本时，直接用原文替换文字、仅借用 ASR 时间轴，再核边界）；
2. 删掉间奏/尾奏里的“啊”等拟声误识别句（参考 `.asr.json` 的 confidence，低置信且处于无歌词区间的多为误识别）；
3. 把被合并的长句按真实句读拆成逐句，核对每句起止边界。

- 用未校对草稿直接切，常报“约束下无解”——长句合并会吃掉合法切点，这是**需要校对的信号，不是去放宽切割算法**。
- 强伴奏/合唱识别过差：先 `mediakit-cli audio separate-voice` 分离出人声轨（先读 `byted-mediakit-audio` 的 separate-voice reference），再对人声轨跑 `transcribe_lyrics.py`。
- `mediakit-cli` 找不到或任务报鉴权错误：需在装有 MediaKit 的豆包环境运行；否则退回请用户提供歌词/字幕。本地自装 faster-whisper 等不是本 skill 的依赖，不默认安装。
- 云端识别会上传音频；涉及未公开/敏感素材时，先告知用户并取得同意。
- 纯音乐/无人声段不应有歌词；草稿里的间奏拟声句删除即可，间奏位置仍以校对后 SRT 的空白段为准。

## 12. 本地多语种对齐（align_lyrics_local.py）

入口：`scripts/align_lyrics_local.py`。当用户给了歌词文本但没有时间轴，尤其是日语/韩语/俄语等云端 ASR 不支持的语种，或需要高精度时，**默认走它**。它不做“识别歌词”，而是用 wav2vec2 CTC 对**用户提供的原文**做强制对齐（forced alignment），因此文字 100% 用原文，只定位时间。

### 12.1 依赖与安装

- `transformers`、`faster-whisper`、`fugashi`+`unidic-lite`（日语注音）、`numpy`：`pip install transformers faster-whisper fugashi unidic-lite`。
- `torch`+`torchaudio`：CPU 版即可，**不要默认装 CUDA 版**（大、慢）。
- 模型首次运行自动从 HuggingFace 下载；国内务必先 `export HF_ENDPOINT=https://hf-mirror.com`。
- 音频统一用 ffmpeg 转 16k mono wav 再 numpy 读取（脚本已处理）。

**torch CPU 国内安装（已实测，官方源和多数镜像会卡/403/给 CUDA 版）：**

1. 官方 `download.pytorch.org/whl/cpu` 常超时；清华 pypi 的大 wheel 403；阿里 pypi simple 给 CUDA 版且慢。
2. 可用路径：阿里云 pytorch 扁平目录 `https://mirrors.aliyun.com/pytorch-wheels/cpu/`（**不是** PEP 503 simple 索引，用 `--index-url` 会报 “No matching distribution”）。
3. 正确做法：浏览器或 `curl` 打开该目录页，找到与 Python 版本匹配的 `torch-<ver>+cpu-cpXXX-...whl`、`torchaudio-<ver>+cpu-cpXXX-...whl`，**直接下载 wheel 后本地安装**：
   ```
   pip install ./torch-2.11.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
   pip install ./torchaudio-2.11.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
   ```
   注意 URL 里的 `+` 需编码成 `%2B`；目录页 HTML 里 `+` 可能显示为 `&#43;`。装完用 `python -c "import torch;print(torch.__version__, torch.cuda.is_available())"` 确认是 `+cpu`、`cuda False`。

**PyAV 兼容（faster-whisper 读文件报 `metadata_errors`）：** 新版 PyAV 与 faster-whisper 的 `decode_audio` 不兼容，`transcribe(文件路径)` 会抛 `open() got an unexpected keyword argument 'metadata_errors'`。规避：**不要传文件路径**，统一用 ffmpeg 转 16k mono wav，再用 numpy 读出数组传给 `transcribe(ndarray)`（脚本已这样做）。读 wav 用 `np.frombuffer(...,np.int16)` 后跳过 44 字节头（`[22:]` 个 int16 采样）。

**faster-whisper 参数：** 1.2.x 用 `vad_parameters=`（不是 `vad_options`/`vad_params`）；尾段定位要 `vad_filter=False` + `initial_prompt=<正确尾句>`。

### 12.2 已验证死路（勿退化）

1. **云端 ASR 不支持日语**：`mediakit-cli video asr-subtitles` 只有 cmn-Hans-CN / eng-US；`--language auto` 把日语音节误识别成英文/罗马音，低置信、草稿不可用。
2. **不对原始混音直接识别/对齐**：直接对原混音跑 faster-whisper 会大量漏句（实测只识别到 20 多秒就漏）。**先 `audio separate-voice` 分离人声**是决定性提升；分离人声后 medium 仍可能漏副歌，large-v3 主歌词级锚点准确，但大合唱齐唱段仍稀疏。
3. **齐唱结尾全曲 CTC 会“摊薄”**：结尾大合唱常把最后副歌尾句再唱一遍（音频两遍、文本只有一遍）。全曲一次 CTC 会把单遍文本摊到两遍，越往后越慢（实测末 4 句被错放到 148/157/166 而真首遍是 140/143/146 或过门后的 151）。脚本已自动“取首遍”，不要把第二遍时间当歌词。
4. **尾段检测窗不能太长**：窗口若一直延伸到曲末（>约 34s，含第二遍后大段尾奏/静音），Whisper 会把第二遍幻觉成“花火/ラララ”等，反而检测失败。默认 `--tail-win 34`；先在短窗内定位首遍结束点，再只对“首遍音频”对齐。
5. **贪心顺序匹配会因单字注音差异雪崩**：fugashi 对整句注音正确（星空→ほしぞら、思い出の出→で），但对 Whisper 切出的单字可能取音读（空→そら、出→しゅつ、永→なが）。必须用全局 DTW（脚本 `dtw_map`）容忍插入/删除/替换，不能逐字贪心。
6. **长过门/无词拖音不是唱句**：副歌与 Outro 之间常有数秒过门（人声轨里有中等能量的和声/气声残留，但 CTC 帧级几乎全 blank、Whisper 多次一致跳过）。`initial_prompt` 过强时 Whisper 偶发会把过门幻觉成下一句首词（提前 3–5 秒）。脚本用“行首锚点 + CTC 门控（锚点不得早于 CTC 行首 1s）”剔除这类幻觉，**判断行首以 CTC 帧级音节与多次 Whisper 一致为准，不要采信心声/气声里的单个词**。
7. **汉字单字 Whisper 行首**：纯首遍短窗里行首汉字（星/永/約）稳定，含第二遍的长窗可能把上一句尾音误判成首行字（如把英文 “Summer” 听成“星”）。门控以 CTC 行首为准，并用“只在首遍区间、时间单调”匹配；尾句若是假名开头（また…），用词首假名匹配。

### 12.3 输入歌词约定

- txt 每行一句；独占一行的 `[Verse]`/`[Chorus]`/`[Mikoto]`/`[Both]` 等段落/角色标记会被自动剔除，行内 `[xxx]` 也会被去除。
- **一行若包含两个短句（如开场“せーの！ 夏が来た！”在同一物理行），需先手工拆成两行**，否则只会得到一个时间戳、两句被并成一条唱句。
- JSON 也支持：`[["句1",""], ...]` 或 `[{"text":"句","sec":""}, ...]`。
- 日语少量英文按日式发音映射：`--en-kana "summer=さまー,days=でいず"`，否则英文词在日语 CTC 词表里是 unk。

### 12.4 其他语种

- 内置 ja/zh/en 模型（见脚本 `ALIGN_MODELS`）；其他语种传 `--align-model <HF id>` 指定一个**字符/假名级 wav2vec2 CTC 模型**（注意：英文这类字母级模型的 token 化与日语不同，`--language` 与模型要匹配）。
- 中文/英文若云端 ASR 草稿质量不够，同样可直接用本地对齐（只信原文、不识别字）。
- 产物 `.align.json` 含行级与字级时间（`rows[].chars`），可直接核对；终端会打印能量校验结果（行起点落在低能量区即警告）。

### 12.5 对齐后仍要跑切割自检

拿到 `.srt` 后照常用 `cut_by_lyrics.py --plan` 确认“进入唱词的切点=0、唱句全部完整”，再实切。日语 SRT 与解析器无关、可直接使用。
