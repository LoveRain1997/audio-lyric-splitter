#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align_lyrics_local.py — 本地多语种歌词时间轴（无歌词时间轴时的默认管线）。

为什么存在：
  云端 mediakit-cli ASR 只支持中/英文，且强伴奏/大合唱/结尾齐唱普遍漏句、合并句。
  本脚本用「分离人声 → faster-whisper 取结构/语种 → wav2vec2-CTC 对【给定原文】做强制对齐
  → 结尾齐唱重复自动取首遍 → 能量包络逐行校验」产出可直接给 cut_by_lyrics.py 的逐句 SRT，
  以及同名 LRC。文字以用户给的原文为准（不依赖识别文本），识别只负责“在哪里唱”。

输入：
  --audio   原始音频（mp3/m4a/wav…）
  --lyrics  歌词文本，两种格式：
              1) 纯文本 .txt：每行一句唱词；空行与纯 [分节]/[角色] 标记行自动忽略；行内 [角色] 片段会被删除。
                 最佳实践：一行一句（像「せーの！ 夏が来た！」这种一行两个短句，请先手工拆成两行）。
              2) .json：[[text, section], ...]，逐句数组（section 仅用于分节，可空串）。
  输出：<base>.lrc、<base>.srt、<base>.align.json（逐句+逐字时间，供检查 / manifest）。

常用：
  # 日语歌（云端 ASR 不支持）：默认自动分离人声、large-v3、日语 CTC 对齐
  python align_lyrics_local.py --audio song.mp3 --lyrics lyrics.txt --language ja
  # 已有分离好的人声、跳过人声分离与全曲 whisper（语种已显式指定），更快
  python align_lyrics_local.py --audio song.mp3 --voice voice_16k.wav --lyrics lyrics.txt --language ja --no-whisper
  # 中/英仍推荐云端 transcribe_lyrics.py（更快）；本脚本也可做高精度兜底
  python align_lyrics_local.py --audio song.mp3 --lyrics lyrics.txt --language zh
  # 其他语种：自带字符型 CTC 模型
  python align_lyrics_local.py --audio a.mp3 --lyrics l.txt --language ko --align-model kresnik/wav2vec2-large-xlsr-korean

环境：
  ffmpeg/ffprobe、mediakit-cli（人声分离；豆包环境内置）。
  pip: faster-whisper torch torchaudio transformers fugashi unidic-lite numpy
  日语需 fugashi + unidic-lite；模型从 HF 下载，国内先 export HF_ENDPOINT=https://hf-mirror.com
  torch CPU 国内安装见 references/troubleshooting.md 第 12 节。
"""
import argparse, json, os, re, shutil, subprocess, sys, wave

# ----------------------------- 依赖检查 -----------------------------
def _require(mods, hint):
    missing = []
    for m in mods:
        try:
            __import__(m)
        except Exception:
            missing.append(m)
    if missing:
        sys.exit(f"缺少依赖 {missing}。\n{hint}")

# ----------------------------- 人声分离（mediakit-cli） -----------------------------
def run_cli(args, timeout=900):
    p = subprocess.run(["mediakit-cli"] + args, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    if p.returncode != 0:
        raise RuntimeError(out[-1200:])
    i, j = out.find("{"), out.rfind("}")
    if i < 0:
        raise RuntimeError("CLI 未返回 JSON：" + out[-500:])
    return json.loads(out[i:j + 1])

def separate_voice(audio, workdir, fmt="wav"):
    cli = shutil.which("mediakit-cli")
    if not cli:
        print("[warn] 未找到 mediakit-cli，跳过人声分离，直接用原音频（强伴奏下对齐可能变差）。")
        return None
    print("[1/6] 提交云端人声分离…")
    resp = run_cli(["audio", "separate-voice", "--audio-url", os.path.abspath(audio),
                    "--output-format", fmt])
    tid = resp.get("task_id")
    if not tid:
        raise RuntimeError("separate-voice 未返回 task_id：" + json.dumps(resp, ensure_ascii=False))
    res = run_cli(["shared", "query-task", "--task-id", tid, "--poll-complete",
                   "--poll-interval-seconds", "5"])
    url = res.get("voice_audio_url")
    if not url:
        raise RuntimeError("separate-voice 未返回 voice_audio_url：" + json.dumps(res, ensure_ascii=False)[:800])
    out = os.path.join(workdir, "_voice_sep." + fmt)
    subprocess.run(["curl", "-sL", "--retry", "3", "-o", out, url], check=True)
    print("    人声轨 ->", out)
    return out

def to_16k_mono(src, dst):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "1", "-ar", "16000",
                    "-f", "wav", dst], check=True)
    return dst

def read_wav(path):
    w = wave.open(path, "rb")
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return x, sr

# ----------------------------- 歌词文本处理 -----------------------------
_TAGLINE = re.compile(r"^\s*\[[^\]]*\]\s*$")
_INLINETAG = re.compile(r"\[[^\]]*\]")
_PUNCT = set('！？!?。、，,.…“”‘’「」『』（）()【】《》〈〉 \t　\n\r<>:;：；-・~～〜♪‘’"#')

def load_lines(path):
    if path.lower().endswith(".json"):
        data = json.load(open(path, encoding="utf-8"))
        out = []
        for item in data:
            if isinstance(item, (list, tuple)):
                txt = item[0]; sec = item[1] if len(item) > 1 else ""
            elif isinstance(item, dict):
                txt = item.get("text"); sec = item.get("sec", "")
            else:
                txt, sec = str(item), ""
            txt = _INLINETAG.sub("", txt).strip()
            if txt:
                out.append((txt, sec or ""))
        return out
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit("无法以 utf-8/gbk 解码歌词文件。")
    out = []
    for ln in text.splitlines():
        if _TAGLINE.match(ln):
            continue
        ln = _INLINETAG.sub("", ln).strip()
        if ln:
            out.append((ln, ""))
    return out

# ----------------------------- 文本 -> 发音 token -----------------------------
K2H = lambda c: chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c
# 日语歌里少量英文的日式发音兜底（可按需扩展；也可用 --en-kana 追加）
EN_KANA = {"summer": "さまー", "days": "でいず", "day": "でい",
           "love": "らぶ", "you": "ゆー", "baby": "べいびー", "my": "まい"}

class Tokenizer:
    def __init__(self, language, en_kana=None):
        self.language = language
        self.en_kana = dict(EN_KANA)
        if en_kana:
            for kv in en_kana.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    self.en_kana[k.strip().lower()] = v.strip()
        self._tagger = None
        if language == "ja":
            _require(["fugashi", "unidic_lite"], "日语转假名需要：pip install fugashi unidic-lite")
            import unidic_lite
            from fugashi import Tagger
            self._tagger = Tagger(f"-d {unidic_lite.DICDIR}")

    def _ja_word_pron(self, seg):
        toks = []
        for w in self._tagger(seg):
            pron = ""
            try:
                pron = w.feature.pron or ""
            except Exception:
                pron = ""
            if not pron or pron == "*":
                pron = w.surface
            for ch in pron:
                if ch in _PUNCT:
                    continue
                if 0x30A0 <= ord(ch) <= 0x30FF:
                    toks.append(K2H(ch))
                elif 0x3040 <= ord(ch) <= 0x309F:
                    toks.append(ch)
        return toks

    def tokens(self, text):
        """返回发音/字符 token 列表（去标点空白）。日语->平假名；其他语种->原文字符。"""
        out = []
        if self.language == "ja":
            for m in re.finditer(r"[A-Za-z]+|[^A-Za-z]+", text):
                seg = m.group(0)
                if re.fullmatch(r"[A-Za-z]+", seg):
                    out.extend(list(self.en_kana.get(seg.lower(), "")))
                else:
                    out.extend(self._ja_word_pron(seg))
            return [t for t in out if t not in _PUNCT and t != ""]
        # 通用：中文汉字 / 英文字母，逐字符
        for ch in text:
            if ch in _PUNCT:
                continue
            if re.fullmatch(r"[A-Za-z]", ch):
                out.append(ch.lower())
            elif ch.strip():
                out.append(ch)
        return out

# ----------------------------- Whisper（结构 + 尾段首遍） -----------------------------
def whisper_words(model_size, audio, language, vad=True, initial_prompt=None):
    from faster_whisper import WhisperModel
    m = WhisperModel(model_size, device="cpu", compute_type="int8")
    kw = dict(word_timestamps=True, beam_size=5, condition_on_previous_text=False)
    if language:
        kw["language"] = language
    if vad:
        kw["vad_filter"] = True
        kw["vad_parameters"] = dict(min_silence_duration_ms=300, speech_pad_ms=120)
    else:
        kw["vad_filter"] = False
    if initial_prompt:
        kw["initial_prompt"] = initial_prompt
        kw["condition_on_previous_text"] = True
    segs, info = m.transcribe(audio, **kw)
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append((t, float(w.start), float(w.end)))
    lang = getattr(info, "language", language)
    return words, lang

def hyp_stream(words, tok):
    """whisper 词 -> (发音 token, 时间)，词内多 token 均匀插值。"""
    H = []
    for wt, s, e in words:
        ts = tok.tokens(wt)
        if not ts:
            continue
        if len(ts) == 1:
            H.append((ts[0], s))
        else:
            for i, t in enumerate(ts):
                H.append((t, s + (e - s) * i / (len(ts) - 1)))
    return H

def _tok_cost(a, b):
    if a == b:
        return 0.0
    if a == "ー" or b == "ー":
        return 0.6
    if {a, b} <= {"じ", "ず", "ぢ", "づ"}:
        return 0.3
    if {a, b} <= {"お", "を"} or {a, b} <= {"え", "へ"}:
        return 0.3
    return 1.0

def match_seq(R, H, start=0, look=12, thresh=0.6):
    """把参考发音序列 R 在假设流 H 上顺序匹配（贪心），返回每个 R 位置的 H 下标或 None。"""
    res = [None] * len(R)
    hp = start
    for i, r in enumerate(R):
        best, bestd = None, thresh + 1e-6
        for j in range(hp, min(hp + look, len(H))):
            c = _tok_cost(r, H[j][0])
            if c < bestd:
                bestd, best = c, j
        if best is not None and bestd <= thresh:
            res[i] = best
            hp = best + 1
    return res

def dtw_map(R, H, ins=1.0, dele=1.0, good=0.5):
    """全局 DTW：把参考发音 R 对齐到假设流 H（H 项为 (token,time)）。
    返回每个 R 位置匹配到的 H 下标（无好匹配则 None）。容忍单字注音差异与漏字（插入/删除）。"""
    import numpy as np
    N, M = len(R), len(H)
    dp = np.full((N + 1, M + 1), np.inf)
    bk = np.zeros((N + 1, M + 1), dtype=np.int8)  # 1 对角 2 删参考 3 插假设
    dp[0, 0] = 0
    for i in range(1, N + 1):
        dp[i, 0] = i * dele; bk[i, 0] = 2
    for j in range(1, M + 1):
        dp[0, j] = j * ins; bk[0, j] = 3
    for i in range(1, N + 1):
        a = R[i - 1]
        for j in range(1, M + 1):
            diag = dp[i - 1, j - 1] + _tok_cost(a, H[j - 1][0])
            up = dp[i - 1, j] + dele
            left = dp[i, j - 1] + ins
            m = min(diag, up, left)
            dp[i, j] = m
            bk[i, j] = 1 if m == diag else (2 if m == up else 3)
    i, j = N, M
    mp = [None] * N
    while i > 0 or j > 0:
        b = bk[i, j]
        if b == 1:
            if dp[i, j] - dp[i - 1, j - 1] < good:
                mp[i - 1] = j - 1
            i -= 1; j -= 1
        elif b == 2:
            i -= 1
        else:
            j -= 1
    return mp

# ----------------------------- CTC 强制对齐 -----------------------------
ALIGN_MODELS = {
    "ja": "jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
    "zh": "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn",
    "en": "jonatasgrosman/wav2vec2-large-xlsr-53-english",
}

def ctc_forced(bundle, audio, sr, token_ids, t_offset=0.0):
    """对一段音频与一串目标 token id 做 CTC 强制对齐。
    返回每个 token 的 (start,end) 全局时间（加 t_offset）。"""
    import torch
    from torchaudio.functional import forced_align, merge_tokens
    model, proc = bundle
    inp = proc(audio, sampling_rate=sr, return_tensors="pt").input_values
    with torch.no_grad():
        logits = model(inp).logits[0]
    em = torch.log_softmax(logits, dim=-1)
    frames = em.shape[0]
    stride = (len(audio) / sr) / max(frames, 1)
    targets = torch.tensor(token_ids, dtype=torch.long)[None]
    aligned, scores = forced_align(em[None], targets, blank=0)
    spans = merge_tokens(aligned[0], scores[0], blank=0)
    out = [None] * len(token_ids)
    for k, sp in enumerate(spans):
        if k < len(out):
            out[k] = (t_offset + sp.start * stride, t_offset + sp.end * stride)
    return out

def tokenize_lines(lines, tok, tker, warn=True):
    """把各行文本转成 CTC token id，返回 (ids, line_span, alltok)。"""
    line_span, alltok, ids = [], [], []
    oov = set()
    unk = tker.unk_token_id
    for li, item in enumerate(lines):
        text = item[0]
        ts = tok.tokens(text)
        line_span.append((li, len(alltok), len(alltok) + len(ts), text))
        for c in ts:
            alltok.append(c)
            i = tker.convert_tokens_to_ids(c)
            if i is None or i == unk:
                oov.add(c); i = unk
            ids.append(i)
    if warn and oov:
        print(f"    [warn] {len(oov)} 个目标字符不在 CTC 词表（按 unk，少量不影响行级）：{sorted(oov)[:20]}")
    return ids, line_span, alltok

def ctc_align(audio, sr, lines, tok, language, align_model, hf_endpoint):
    from transformers import Wav2Vec2ForCTC, AutoProcessor
    model_id = align_model or ALIGN_MODELS.get(language)
    if not model_id:
        sys.exit(f"未内置语种 {language} 的 CTC 模型，请用 --align-model 指定字符型 wav2vec2 模型。")
    print(f"[3/6] 加载 CTC 对齐模型：{model_id}")
    proc = AutoProcessor.from_pretrained(model_id)
    model = Wav2Vec2ForCTC.from_pretrained(model_id).eval()
    bundle = (model, proc)
    ids, line_span, alltok = tokenize_lines(lines, tok, proc.tokenizer)
    char_t = ctc_forced(bundle, audio, sr, ids)
    rows = []
    for li, s, e, text in line_span:
        cs = [c for c in char_t[s:e] if c]
        st = cs[0][0] if cs else None
        en = cs[-1][1] if cs else None
        rows.append({"line": li, "start": st, "end": en, "text": text,
                     "sec": lines[li][1], "chars": char_t[s:e]})
    return rows, (len(audio) / sr) / max(len(ids), 1), bundle

# ----------------------------- 尾段齐唱重复：取首遍 -----------------------------
def tail_first_pass(rows, lines, tok, voice, sr, language, model_size, bundle,
                    tail_lines=4, win_pad=1.5, win_cap=34.0, post_dur=13.0,
                    rms_thresh=0.045):
    """结尾大合唱常把最后副歌尾句再唱一遍（音频两遍、文本一遍）。
    CTC 全曲对齐会把单遍文本摊到两遍、越往后越慢。这里：
      1) 尾窗 Whisper 用 R+R 两遍 DTW 定位首遍边界（第二遍首音 / 首遍末音），并用首遍后的持续人声能量兜底；
      2) 只对“首遍音频”用 CTC 重新精确对齐最后 K 行（CTC 不依赖识别，避免长拖音字被漏）。
    """
    import numpy as np
    if len(rows) < tail_lines:
        return rows, None
    k0 = len(rows) - tail_lines
    starts = [r["start"] for r in rows[k0:] if r["start"] is not None]
    if not starts:
        return rows, None
    win0 = max(0.0, min(starts) - win_pad)
    win1 = min(len(voice) / sr, win0 + win_cap)   # 长窗齐唱会让 whisper 幻觉/漂移，窗口封顶
    seg = voice[int(win0 * sr):int(win1 * sr)]
    prompt = " ".join(t for t, _ in lines[k0:])
    print(f"[4/6] 尾段（{win0:.1f}-{win1:.1f}s）齐唱重复检测，定位首遍边界…")
    words, _ = whisper_words(model_size, seg, language, vad=False, initial_prompt=prompt)
    H = [(t, s + win0) for t, s in hyp_stream([(w, s, e) for w, s, e in words], tok)]  # 转全局时间
    R, belong = [], []
    for li in range(k0, len(rows)):
        for c in tok.tokens(lines[li][0]):
            R.append(c); belong.append(li)
    if not R or not H:
        return rows, None
    n = len(R)
    mp = dtw_map(R + R, H)
    mp1, mp2 = mp[:n], mp[n:]
    m1 = sum(j is not None for j in mp1) / n
    m2 = sum(j is not None for j in mp2) / n
    first_t = [H[j][1] for j in mp1 if j is not None]
    t_end = max(first_t) if first_t else None
    # 第二遍首音（仅用于报告/参考）：第二遍第一行 li=k0 的首个匹配音
    t_sec = None
    for p in range(n):
        if belong[p] == k0 and mp2[p] is not None:
            t_sec = H[mp2[p]][1]
            break
    # 首遍结束后持续演唱的能量占空比（第二遍被 whisper 漏识别时兜底）
    occ = 0.0
    if t_end is not None:
        a = int((t_end + 0.4) * sr)
        b = int(min(t_end + 0.4 + post_dur, win1) * sr)
        if b > a:
            seg2 = voice[a:b]; hop = int(0.02 * sr)
            rr = np.array([np.sqrt(np.mean(seg2[i * hop:(i + 1) * hop] ** 2) + 1e-9)
                           for i in range(len(seg2) // hop)])
            occ = float(np.mean(rr > rms_thresh))
    has_second = (m2 >= 0.5 and t_sec is not None) or occ >= 0.5
    detected = (m1 >= 0.5) and has_second
    info = {"window": [round(win0, 2), round(win1, 2)], "first_match": round(m1, 2),
            "second_match": round(m2, 2), "second_start": round(t_sec, 2) if t_sec else None,
            "first_end": round(t_end, 2) if t_end else None,
            "post_voice_occupy": round(occ, 2), "detected": bool(detected)}
    if not detected:
        print(f"    未检测到重复（首遍 {m1:.2f} / 第二遍 {m2:.2f} / 尾后人声 {occ:.2f}），保留 CTC。")
        return rows, info
    # 首遍音频右边界：首遍末音结束 +0.3s（气口再窄也只影响末字 end，不影响各行 start）。
    t_cut = (t_end + 0.3) if t_end is not None else win1
    t_cut = min(max(t_cut, win0 + 3.0), win1)
    print(f"    检测到结尾齐唱重复（首遍 {m1:.2f} / 第二遍 {m2:.2f} / 尾后人声 {occ:.2f} / "
          f"首遍切到 {t_cut:.2f}s）；双窗行首锚点 + CTC 门控取首遍。")
    # CTC 对只含首遍的音频段重对齐，作为行首“门控/兜底”（过门 prompt 幻觉与前句误判会被剔除）
    seg1 = voice[int(win0 * sr):int(t_cut * sr)]
    ctc_rows = {}
    sub = lines[k0:]
    ids, line_span, _ = tokenize_lines(sub, tok, bundle[1].tokenizer)
    if ids:
        char_t = ctc_forced(bundle, seg1, sr, ids, t_offset=win0)
        for local_li, s, e, _t in line_span:
            cs = [c for c in char_t[s:e] if c]
            if cs:
                ctc_rows[k0 + local_li] = (float(cs[0][0]), float(cs[-1][1]))
    punct = set("！？。、…!?,，．・「」『』 \t　")
    GATE = 1.0  # 锚点行首不得早于 CTC 行首超过此秒数（否则判过门幻觉/前句误判）

    def anchor_starts(W):
        """在一组（全局时间）词里按行首锚点、时间单调匹配；CTC 门控剔除过早幻觉，缺失回退 CTC。"""
        out = {}
        cursor = 0
        for gi in range(k0, len(rows)):
            anchor = next((c for c in lines[gi][0] if c and c not in punct), lines[gi][0][:1])
            is_kana = (ord(anchor) >= 0x3040)
            cstart = ctc_rows.get(gi, (None, None))[0]
            for j in range(cursor, len(W)):
                wt, ts, te = W[j]
                if ts >= t_cut:
                    break
                if not (wt.startswith(anchor) if is_kana else anchor in wt):
                    continue
                if cstart is not None and ts < cstart - GATE:
                    continue
                out[gi] = (float(ts), float(te)); cursor = j + 1
                break
            if gi not in out and cstart is not None:
                out[gi] = ctc_rows[gi]
        return out

    # 长窗（含第二遍，また等尾句起音稳定）；再对纯首遍短窗识别一次（开头行更准）
    W_long = [(w, s + win0, e + win0) for w, s, e in words]
    cand = [anchor_starts(W_long)]
    try:
        w1, _ = whisper_words(model_size, seg1, language, vad=False, initial_prompt=prompt)
        W_short = [(w, s + win0, e + win0) for w, s, e in w1]
        cand.append(anchor_starts(W_short))
    except Exception as ex:
        print("    [warn] 纯首遍短窗识别失败，仅用长窗：", str(ex)[:120])
    # 合并：每行取所有通过门控的候选里最早的行首（CTC 倾向偏晚，真起音略早，门控已保证下限）
    starts = {}
    for gi in range(k0, len(rows)):
        vals = [c[gi] for c in cand if gi in c]
        if vals:
            starts[gi] = min(vals, key=lambda x: x[0])
    # 行尾由下一行行首决定（末行到 t_cut），保证不跨行
    ks = sorted(starts)
    for n2, gi in enumerate(ks):
        t0 = starts[gi][0]
        nxt = starts[ks[n2 + 1]][0] if n2 + 1 < len(ks) else t_cut - 0.1
        rows[gi]["start"] = round(t0, 3)
        rows[gi]["end"] = round(max(nxt - 0.1, t0 + 0.2), 3)
        rows[gi]["tail_fixed"] = True
    return rows, info

# ----------------------------- 能量校验 -----------------------------
def energy_check(voice, sr, rows):
    import numpy as np
    hop = int(0.02 * sr)
    rms = np.array([np.sqrt(np.mean(voice[i * hop:(i + 1) * hop] ** 2) + 1e-9)
                    for i in range(len(voice) // hop)])
    def at(t):
        i = int(t / 0.02)
        return rms[max(0, i - 10):i + 10].max()
    low = []
    for r in rows:
        if r["start"] is None:
            low.append((r["line"], "NO-TIME"))
            continue
        v = at(r["start"])
        r["start_rms"] = round(float(v), 3)
        if v < 0.045:
            low.append((r["line"], round(float(v), 3)))
    return low

# ----------------------------- 输出 -----------------------------
def srt_t(t):
    t = max(0.0, float(t)); h = int(t // 3600); m = int((t % 3600) // 60)
    s = int(t % 60); ms = int(round((t - int(t)) * 1000))
    if ms == 1000: ms = 0; s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
def lrc_t(t):
    t = max(0.0, float(t)); m = int(t // 60); s = t - 60 * m
    return f"[{m:02d}:{s:05.2f}]"

def write_outputs(rows, total, base, title, language):
    # 单调化与缺失补齐
    last = -1.0
    for i, r in enumerate(rows):
        if r["start"] is None:
            r["start"] = round(last + 0.5, 3)
        if r["start"] < last:
            r["start"] = round(last + 0.05, 3)
        last = r["start"]
    # 句末：本行最后字 end，clamp 到下一行 start 前
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["start"] if i + 1 < len(rows) else total
        en = r["end"] if r["end"] is not None else min(nxt - 0.05, r["start"] + 1.0)
        if en <= r["start"]:
            en = min(nxt - 0.05, r["start"] + 0.8)
        if i + 1 < len(rows) and en > nxt - 0.02:
            en = nxt - 0.02
        r["end"] = round(max(en, r["start"] + 0.2), 3)

    lrc = [f"[ti:{title}]", f"[la:{language}]", f"[length:{int(total//60):02d}:{total%60:05.2f}]"]
    srt = []
    for n, r in enumerate(rows, 1):
        lrc.append(lrc_t(r["start"]) + r["text"])
        srt.append(f"{n}\n{srt_t(r['start'])} --> {srt_t(r['end'])}\n{r['text']}\n")
    open(base + ".lrc", "w", encoding="utf-8").write("\n".join(x for x in lrc if x) + "\n")
    open(base + ".srt", "w", encoding="utf-8").write("\n".join(srt))
    json.dump({"total": total, "title": title, "rows": rows},
              open(base + ".align.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return base + ".lrc", base + ".srt", base + ".align.json"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--lyrics", required=True)
    ap.add_argument("--language", default="ja", help="ja/zh/en/ko…；CTC 模型按语种选")
    ap.add_argument("--align-model", default="", help="自定义字符型 wav2vec2 CTC 模型 HF id")
    ap.add_argument("--whisper-model", default="large-v3")
    ap.add_argument("--voice", default="", help="已分离/处理好的 16k 人声 wav；给定则跳过分离")
    ap.add_argument("--no-separate", action="store_true", help="不做人声分离，直接用原音频")
    ap.add_argument("--no-whisper", action="store_true", help="跳过全曲 whisper（语种已显式指定时）")
    ap.add_argument("--no-tail-fix", action="store_true", help="关闭结尾齐唱重复首遍修正")
    ap.add_argument("--tail-lines", type=int, default=4, help="结尾参与重复检测的句数（重复的最后副歌尾句，通常 4 句）")
    ap.add_argument("--tail-win", type=float, default=34.0, help="尾段检测窗口最长秒数（过长齐唱会让 whisper 漂移/幻觉）")
    ap.add_argument("--en-kana", default="", help="追加英文片假名映射，如 'hello=はろー,world=わーるど'")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="", help="输出文件 basename（默认同音频名）")
    args = ap.parse_args()

    _require(["numpy"], "需要 numpy：pip install numpy")
    global np
    import numpy as np

    workdir = os.path.dirname(os.path.abspath(args.audio))
    base = args.out or os.path.join(workdir, os.path.splitext(os.path.basename(args.audio))[0])
    title = args.title or os.path.splitext(os.path.basename(args.audio))[0]

    print("[0/6] 读取歌词原文…")
    lines = load_lines(args.lyrics)
    if not lines:
        sys.exit("歌词为空：检查 --lyrics（每行一句）。")
    print(f"    共 {len(lines)} 句。")
    tok = Tokenizer(args.language, args.en_kana)

    # 人声轨
    if args.voice and os.path.exists(args.voice):
        voice_path = args.voice
        if not voice_path.endswith(".wav"):
            voice_path = to_16k_mono(voice_path, os.path.join(workdir, "_voice16k.wav"))
        else:
            voice_path = to_16k_mono(voice_path, os.path.join(workdir, "_voice16k.wav"))
    else:
        sep = None if args.no_separate else separate_voice(args.audio, workdir)
        src = sep or args.audio
        voice_path = to_16k_mono(src, os.path.join(workdir, "_voice16k.wav"))
    voice, sr = read_wav(voice_path)
    total = len(voice) / sr

    language = args.language
    if not args.no_whisper:
        print("[2/6] Whisper 全曲识别（结构/语种锚点）…")
        _require(["faster_whisper"], "需要 faster-whisper：pip install faster-whisper")
        _w, language = whisper_words(args.whisper_model, voice, args.language or None, vad=True)
        print(f"    语种={language}，识别词数={len(_w)}")

    rows, stride, bundle = ctc_align(voice, sr, lines, tok, language, args.align_model or None,
                                     os.environ.get("HF_ENDPOINT", ""))
    tail_info = None
    if not args.no_tail_fix and len(rows) >= 4:
        _require(["faster_whisper"], "尾段修正需要 faster-whisper；或加 --no-tail-fix")
        try:
            rows, tail_info = tail_first_pass(rows, lines, tok, voice, sr, language,
                                              args.whisper_model, bundle,
                                              tail_lines=args.tail_lines, win_cap=args.tail_win)
        except Exception as ex:
            print("[warn] 尾段修正失败，保留 CTC：", str(ex)[:200])

    print("[5/6] 能量包络逐行校验…")
    low = energy_check(voice, sr, rows)
    if low:
        print("    [warn] 以下行起点落在低能量区（疑似静音/间奏，请人工核对）：", low)
    else:
        print("    全部行起点均落在有声区。")

    lrc, srt, al = write_outputs(rows, total, base, title, language)
    print("[6/6] 输出：")
    print("   ", lrc)
    print("   ", srt)
    print("   ", al)
    if tail_info:
        print("    tail:", json.dumps(tail_info, ensure_ascii=False))
    print("\n下一步：python cut_by_lyrics.py --audio '%s' --srt '%s' --outdir out --plan"
          % (args.audio, srt))

if __name__ == "__main__":
    main()
