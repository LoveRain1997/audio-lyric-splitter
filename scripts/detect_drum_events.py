# -*- coding: utf-8 -*-
"""
detect_drum_events.py — 基于真实音频反推鼓组/起音事件（不编造，全部可复现）。

管线：
  1) 输入 demucs(htdemucs) 分离出的鼓轨 drums.wav（真实鼓来源，去除人声/旋律乐器）；
  2) librosa 计算 onset 强度包络并做离散峰值检测（onset_detect, delta）；
  3) 以 ~161.5 BPM 倍速网格做节奏门限，只保留贴合网格（容差内）的事件；
  4) 对每个事件取频谱特征[低频占比/中频占比/高频占比/质心/平坦度]，KMeans 4 类，
     按特征物理含义映射 kick / snare / hihat / other；
  5) 置信度 = onset 强度分位 + 网格贴合 + 类型典型性 的综合，附方法与局限。

说明：这是数据驱动的启发式反推，不是分轨鼓转录的逐音真值；other 为不满足三类
典型频谱的打击（可能含 tom/打击乐/弱奏/分离残留），不强行归类。
"""
import argparse, json
import numpy as np, librosa
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans

def detect(drum_path, delta=0.3, grid_tol=0.09, sr=22050, hop=512):
    y, sr = librosa.load(drum_path, sr=sr, mono=True)
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    # 节拍网格（半速 beat 后倍速）
    _, bf = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    bt = librosa.frames_to_time(bf, sr=sr, hop_length=hop)
    grid = np.sort(np.concatenate([bt, (bt[:-1] + bt[1:]) / 2]))
    tempo = float(librosa.feature.tempo(y=y, sr=sr)[0])
    # 离散 onset
    fr = librosa.onset.onset_detect(y=y, sr=sr, onset_envelope=oenv,
                                    hop_length=hop, delta=delta)
    tt = librosa.frames_to_time(fr, sr=sr, hop_length=hop)
    # 网格门限
    gidx = np.array([int(np.argmin(np.abs(grid - t))) for t in tt])
    goff = np.array([tt[i] - grid[gidx[i]] for i in range(len(tt))])
    keep = np.abs(goff) <= grid_tol
    fr, tt, gidx, goff = fr[keep], tt[keep], gidx[keep], goff[keep]
    # 频谱特征
    S = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
    fq = librosa.fft_frequencies(sr=sr, n_fft=1024)
    bm = lambda lo, hi: (fq >= lo) & (fq < hi)
    mk, ms, mh = bm(20, 110), bm(120, 400), bm(6000, 16000)
    cent = librosa.feature.spectral_centroid(S=S, sr=sr)[0]
    flat = librosa.feature.spectral_flatness(S=S)[0]
    X, dists = [], []
    for f in fr:
        a, b = max(0, f - 1), min(S.shape[1], f + 3)
        seg = S[:, a:b].mean(1) + 1e-9; tot = seg.sum()
        X.append([seg[mk].sum() / tot, seg[ms].sum() / tot, seg[mh].sum() / tot,
                  cent[a:b].mean() / sr, flat[a:b].mean()])
    X = np.array(X)
    Xs = StandardScaler().fit_transform(X)
    km = KMeans(4, n_init=10, random_state=0).fit(Xs)
    lab = km.labels_
    cdist = np.min(np.abs(
        Xs[:, None, :] - km.cluster_centers_[None, :, :]).sum(2), axis=1)
    mean = {c: X[lab == c].mean(0) for c in range(4)}
    hi_c = max(mean, key=lambda c: mean[c][4])
    kick_c = max((c for c in mean if c != hi_c), key=lambda c: mean[c][0])
    sn_c = max((c for c in mean if c not in (hi_c, kick_c)), key=lambda c: mean[c][3])
    ot_c = [c for c in mean if c not in (hi_c, kick_c, sn_c)][0]
    name = {kick_c: "kick", sn_c: "snare", hi_c: "hihat", ot_c: "other"}
    # 置信度
    s_rank = oenv[fr] / (np.percentile(oenv, 99) or 1)
    typ_pen = {c: (0.0 if c == ot_c else 0.08) for c in range(4)}
    events = []
    for i in range(len(tt)):
        c = lab[i]
        gpart = 0.1 * (1 - abs(goff[i]) / grid_tol)
        conf = 0.45 + 0.27 * min(s_rank[i], 1) + gpart + typ_pen[c] - 0.05 * min(cdist[i], 2)
        events.append({
            "time": round(float(tt[i]), 4),
            "type": name[c],
            "confidence": round(float(min(conf, 0.97)), 2),
            "strength": round(float(oenv[fr[i]]), 3),
            "beat_index": int(gidx[i]),
            "grid_offset": round(float(goff[i]), 4),
        })
    events.sort(key=lambda e: e["time"])
    from collections import Counter
    meta = {
        "method": "demucs(htdemucs) drum stem + librosa onset_detect + spectral-feature KMeans(4)",
        "source_track": "drums.wav",
        "detected_tempo_bpm": round(tempo * 2 if tempo < 100 else tempo, 2),
        "onset_delta": delta, "grid_tolerance_sec": grid_tol,
        "event_count": len(events),
        "type_counts": dict(Counter(e["type"] for e in events)),
        "confidence_model": "0.45 base + onset-strength rank + grid alignment + type typicality",
        "limitations": "启发式反推非逐音真值；other 含 tom/打击乐/弱奏/分离残留；可能漏弱音；"
                       "实测 tempo~161.5 与原分析 178 网格存在差异，以本检测为准。",
    }
    return events, meta

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--drum", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--delta", type=float, default=0.3)
    ap.add_argument("--grid-tol", type=float, default=0.09)
    args = ap.parse_args()
    events, meta = detect(args.drum, args.delta, args.grid_tol)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print("前 10 事件:")
    for e in events[:10]:
        print("  %.3f %-6s conf %.2f off %+.3f" %
              (e["time"], e["type"], e["confidence"], e["grid_offset"]))
    if args.out:
        json.dump({"detection_meta": meta, "drum_events": events},
                  open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("已写", args.out)
