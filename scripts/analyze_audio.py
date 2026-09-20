#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_audio.py — 音乐 BPM / 首拍 / 节拍网格 / 低能量间奏分析（纯 numpy+scipy，无需 librosa）

用法：
  python analyze_audio.py "song.mp3"
  python analyze_audio.py "song.mp3" --out analysis.json
  python analyze_audio.py "song.mp3" --bpm-low 45 --bpm-high 190

输出 JSON：duration / sample_rate / bpm / beat_sec / bar_sec / first_downbeat_sec /
          bpm_candidates（自相关候选峰）/ tested（半速·倍速试验）/ low_energy_sec（疑似间奏）

说明：
- 自相关给候选 BPM，再对主峰及其半速/倍速做逐帧相位扫描，选 onset 对齐分最高者，规避八度错误。
- 慢板古风/自由散板常出现“半速 vs 加倍”两个都合理的值（如 60.75 与 121.5），自动值仅供参考；
  请结合听感用 cut_by_lyrics.py 的 --bpm / --first-beat 人工锁定。
- 满编曲间奏往往检测不到低能量，间奏位置应以歌词 SRT 的空白段为准，low_energy_sec 仅作辅助。
"""
import argparse, json, os, shutil, subprocess, sys, tempfile
import numpy as np
from scipy import signal
from scipy.io import wavfile

# 可选：PATH 找不到 ffmpeg 时，可在此追加 ffmpeg 绝对路径作为兜底（列表可留空）
FFMPEG_FALLBACK = []
SR = 22050

def find_exe(name):
    p = shutil.which(name)
    if p: return p
    for c in FFMPEG_FALLBACK:
        if os.path.exists(c): return c
    return None

def decode_mono(audio_path):
    ff = find_exe("ffmpeg")
    if not ff:
        sys.exit("未找到 ffmpeg，请先安装并加入 PATH（Windows 可用 winget install Gyan.FFmpeg）。")
    fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        r = subprocess.run([ff, "-y", "-v", "error", "-i", str(audio_path),
                            "-ac", "1", "-ar", str(SR), wav],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            sys.exit("ffmpeg 解码失败：\n" + r.stderr[-600:])
        sr, x = wavfile.read(wav)
        x = x.astype(np.float64)
        if x.size == 0: sys.exit("解码得到空音频。")
        x /= (np.abs(x).max() + 1e-9)
        return sr, x
    finally:
        try: os.remove(wav)
        except OSError: pass

def _onset_envelope(x, sr):
    nper, hop = 2048, 512
    f, t, Z = signal.stft(x, fs=sr, nperseg=nper, noverlap=nper - hop)
    M = np.abs(Z) + 1e-12
    flux = np.maximum(0, np.diff(M, axis=1)).sum(axis=0)
    flux = signal.medfilt(flux, 5)
    env = flux.copy()
    env = (env - env.mean()) / (env.std() + 1e-9)
    env = np.maximum(env, 0)
    return env, sr / hop

def _bpm_candidates(env, fps, low, high):
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    def lag(b): return int(round(fps * 60.0 / b))
    scored = [(ac[lag(b)], b) for b in np.arange(low, high, 0.25)]
    scored.sort(reverse=True)
    peaks = []
    for v, b in scored:
        if all(abs(b - pb) > 3 for _, pb in peaks):
            peaks.append((float(v), float(b)))
        if len(peaks) >= 6: break
    return peaks

def analyze(audio_path, bpm_low=50.0, bpm_high=180.0):
    sr, x = decode_mono(audio_path)
    dur = len(x) / sr
    env, fps = _onset_envelope(x, sr)
    peaks = _bpm_candidates(env, fps, bpm_low, bpm_high)
    if not peaks:
        raise RuntimeError("未能检测到稳定 BPM（可能是散板/长音为主），可用 --bpm 手动指定。")
    base = peaks[0][1]
    trials = sorted({b for b in (base / 2.0, base, base * 2.0) if bpm_low - 5 <= b <= bpm_high + 5})
    nfr = len(env)
    best = None
    for bpm in trials:
        bf = fps * 60.0 / bpm
        for ph in np.arange(0, bf, 1):
            idx = np.round(np.arange(ph, nfr, bf)).astype(int)
            idx = idx[idx < nfr]
            sc = env[np.clip(idx, 0, nfr - 1)].sum() + 0.5 * env[np.clip(idx + 1, 0, nfr - 1)].sum()
            if best is None or sc > best[0]:
                best = (sc, bpm, ph / bf, ph / fps)
    _, bpm, _phase, first_beat = best
    beat = 60.0 / bpm
    # 低能量（疑似间奏）：每秒 RMS
    win = sr
    rms = []
    for s0 in range(0, len(x) - win + 1, win):
        rms.append(float(np.sqrt((x[s0:s0 + win] ** 2).mean())))
    rms = np.array(rms)
    low_segs = []
    if (rms > 0).any():
        med = np.median(rms[rms > 0])
        low = rms < 0.45 * med
        i = 0
        while i < len(low):
            if low[i]:
                j = i
                while j + 1 < len(low) and low[j + 1]: j += 1
                if j - i >= 1: low_segs.append([i, j + 1])
                i = j + 1
            else:
                i += 1
    return {
        "source": os.path.basename(str(audio_path)),
        "duration": round(dur, 3),
        "sample_rate": int(sr),
        "bpm": round(float(bpm), 2),
        "beat_sec": round(beat, 4),
        "bar_sec": round(4 * beat, 4),
        "phrase_sec": round(8 * beat, 4),
        "first_downbeat_sec": round(float(first_beat), 3),
        "bpm_candidates": [round(b, 2) for _, b in peaks],
        "tested_half_double": [round(float(b), 2) for b in trials],
        "low_energy_sec": low_segs,
    }

def main():
    ap = argparse.ArgumentParser(description="分析音乐 BPM/首拍/间奏")
    ap.add_argument("audio", help="音频文件路径（mp3/wav/m4a/flac 等 ffmpeg 支持格式）")
    ap.add_argument("--out", help="将分析结果写入该 JSON 文件")
    ap.add_argument("--bpm-low", type=float, default=50.0)
    ap.add_argument("--bpm-high", type=float, default=180.0)
    args = ap.parse_args()
    if not os.path.exists(args.audio):
        sys.exit(f"找不到音频：{args.audio}")
    info = analyze(args.audio, args.bpm_low, args.bpm_high)
    print(f"时长 {info['duration']:.3f}s | BPM {info['bpm']:.2f} | beat {info['beat_sec']:.4f}s "
          f"| bar(4拍) {info['bar_sec']:.4f}s | 首拍 {info['first_downbeat_sec']:.3f}s")
    print("自相关候选峰:", info["bpm_candidates"], " 半/倍速试验:", info["tested_half_double"])
    print("疑似低能量(间奏)区间(秒):", info["low_energy_sec"] or "无（满编曲间奏请以SRT空白为准）")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print("已写入", args.out)

if __name__ == "__main__":
    main()
