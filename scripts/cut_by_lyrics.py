#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cut_by_lyrics.py — 歌词感知的音乐分段切割（为 H3/AI 视频多段生成准备 10–15s 片段）

核心保证：切点绝不进入任何唱句发声区间内部；每段时长受限；切点尽量落在节拍/句间气口。
输入：一个音频 + 一个逐句歌词 SRT（标准 SubRip：序号 / HH:MM:SS,mmm --> HH:MM:SS,mmm / 歌词）。

用法：
  # 1) 先看方案（不切文件）。BPM 自动分析，也可手动锁定：
  python cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --plan
  python cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --plan --bpm 60.75

  # 2) 实际切割 + 写 cuts_manifest.json（含段内节奏点 beats 与唱句 lyric_events）：
  python cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out

  # 3) 切割并打包 zip：
  python cut_by_lyrics.py --audio "song.mp3" --srt "song.srt" --outdir out --title "歌名"

BPM 来源优先级：--bpm/--first-beat 显式参数 > --analysis JSON > 自动分析（调用同目录 analyze_audio.py）。
"""
import argparse, json, os, re, shutil, subprocess, sys, zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EPS = 0.08
# 可选：PATH 找不到 ffmpeg 时，可在此追加 ffmpeg 绝对路径作为兜底
# （ffprobe 需与之同目录，脚本会按文件名自动推导）；列表可留空
FFMPEG_FALLBACK = []

# ----------------------------- 外部工具 -----------------------------
def find_exe(name):
    p = shutil.which(name)
    if p: return p
    for c in FFMPEG_FALLBACK:
        if os.path.exists(c):
            return c if name == "ffmpeg" else c.replace("ffmpeg.exe", "ffprobe.exe")
    return None

def probe_duration(fp, src):
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "json", str(src)], capture_output=True, text=True, encoding="utf-8")
    return float(json.loads(r.stdout)["format"]["duration"])

# ----------------------------- SRT 解析 -----------------------------
_TC = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_TAG = re.compile(r"<[^>]+>|\{[^}]*\}")
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

def _tc(g):
    g = list(map(int, g))
    return (g[0]*3600 + g[1]*60 + g[2] + g[3]/1000.0,
            g[4]*3600 + g[5]*60 + g[6] + g[7]/1000.0)

def parse_srt(path):
    raw = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                raw = f.read(); break
        except (UnicodeDecodeError, OSError):
            continue
    if raw is None:
        sys.exit(f"无法读取 SRT：{path}")
    out = []
    for block in re.split(r"\r?\n\r?\n+", raw.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        m = _TC.search(lines[ti])
        if not m:
            continue
        s, e = _tc(m.groups())
        body = [_TAG.sub("", ln).strip() for ln in lines[ti+1:]]
        body = [b for b in body if b]
        if not body:
            text = ""
        else:
            joined = "".join(body) if _CJK.search("".join(body)) else " ".join(body)
            text = joined.strip()
        if e > s:
            out.append((round(s, 3), round(e, 3), text))
    out.sort(key=lambda x: x[0])
    if not out:
        sys.exit("SRT 未解析出任何唱句，请检查格式。")
    return out

# ----------------------------- 切割算法 -----------------------------
class Cutter:
    def __init__(self, lyrics, T, bpm, first_beat, min_seg=10.0, max_seg=15.0,
                 target=12.5, guard=0.06, gap_step=0.02):
        self.lyrics = lyrics
        self.IV = sorted((s, e) for s, e, _ in lyrics)
        self.T = T
        self.bpm = bpm; self.beat = 60.0 / bpm; self.bar = 4 * self.beat
        self.fb = first_beat
        self.min_seg, self.max_seg, self.target = min_seg, max_seg, target
        self.guard, self.gap_step = guard, gap_step
        self.OCC = self._merge_occ()

    def inside_any(self, t):
        return any(s < t < e for s, e in self.IV)
    def legal(self, t):
        return not self.inside_any(t)

    def _merge_occ(self):
        out = []
        for s, e in self.IV:
            if out and s <= out[-1][1] + 1e-6:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return [(s, e) for s, e in out]

    def _beat_pref(self, t):
        k = (t - self.fb) / self.beat; kf = round(k)
        if kf < 0: return 0.0
        d = abs(t - (self.fb + kf * self.beat))
        if d > 0.08: return 0.0
        return 1.5 if kf % 4 == 0 else 0.6

    def _build_candidates(self):
        IV, T = self.IV, self.T
        junc = set()
        for i in range(len(IV) - 1):
            if IV[i+1][0] - IV[i][1] < 0.15:
                junc.add(round(IV[i][1], 3))
        gaps = [(0.0, self.OCC[0][0])] + \
               [(self.OCC[i][1], self.OCC[i+1][0]) for i in range(len(self.OCC) - 1)] + \
               [(self.OCC[-1][1], T)]

        def safe(t):
            if not self.legal(t): return False
            if t <= 1e-6 or t >= T - 1e-6: return True
            if any(abs(t - jx) < 0.02 for jx in junc): return True
            for a, b in gaps:
                if a - 1e-9 <= t <= b + 1e-9:
                    if a == 0.0: return t <= b - self.guard
                    if b == T:    return t >= a + self.guard
                    if b - a >= 0.15:
                        return t >= a + self.guard and t <= b - self.guard
                    return False
            return False

        c = {0.0, round(T, 3)}
        k = 0
        while True:
            t = self.fb + k * self.beat
            if t >= T - 0.2: break
            if t > 0.3 and safe(t): c.add(round(t, 3))
            k += 1
        for jx in junc:
            if 0.0 < jx < T and safe(jx): c.add(jx)
        for a, b in gaps:
            if a == 0.0: lo, hi = 0.0, b - self.guard
            elif b == T: lo, hi = a + self.guard, T
            else:
                if b - a < 0.15: continue
                lo, hi = a + self.guard, b - self.guard
            if hi >= -1e-9:
                t = lo
                while t <= hi + 1e-9:
                    if safe(t): c.add(round(t, 3))
                    t += self.gap_step
        return sorted(c)

    def plan(self):
        c = self._build_candidates(); n = len(c); INF = float("inf")
        dp = [INF] * n; pv = [-1] * n; dp[0] = 0
        for i in range(1, n):
            bi = self._beat_pref(c[i])
            for j in range(i):
                L = c[i] - c[j]
                if L < self.min_seg - EPS or L > self.max_seg + EPS or dp[j] == INF:
                    continue
                cost = dp[j] + (L - self.target) ** 2 - bi
                if cost < dp[i]:
                    dp[i], pv[i] = cost, j
        if dp[n-1] == INF:
            raise RuntimeError("约束下无解：可适当放宽 --min/--max，或检查 SRT 时间轴。")
        idx = n - 1; ends = []
        while idx > 0:
            ends.append(idx); idx = pv[idx]
        ends.reverse()
        bd = [0] + [c[i] for i in ends]
        rows = []
        for i in range(len(bd) - 1):
            a, b = bd[i], bd[i+1]
            rows.append(dict(seg=i+1, start=round(a, 3), end=round(b, 3),
                             length=round(b - a, 3), cut=self.inside_any(b)))
        return rows

    def accent(self, k):
        if k % 8 == 0: return "phrase"    # 8拍/2小节 乐句强拍
        if k % 4 == 0: return "downbeat" # 4拍 小节强拍
        if k % 2 == 0: return "onbeat"   # 正拍
        return "offbeat"                  # 弱拍/反拍

    def beats_in_segment(self, a, b):
        out = []
        k0 = int(round((a - self.fb) / self.beat)) - 2
        k1 = int(round((b - self.fb) / self.beat)) + 2
        for k in range(k0, k1 + 1):
            t = self.fb + k * self.beat
            if a - 1e-9 <= t <= b + 1e-9:
                out.append({"local": round(t - a, 3), "global": round(t, 3),
                            "k": k, "accent": self.accent(k)})
        return out

# ----------------------------- 切割/命名/打包 -----------------------------
def hms(t):
    m = int(t // 60); return f"{m:02d}m{t - m*60:05.2f}s".replace(".", "p")

def cut_one(ff, src, s, e, out, bitrate="192k"):
    r = subprocess.run([ff, "-y", "-v", "error", "-i", str(src), "-ss", f"{s:.3f}",
                        "-to", f"{e:.3f}", "-c:a", "libmp3lame", "-b:a", bitrate,
                        "-map_metadata", "-1", str(out)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode: raise RuntimeError(r.stderr[-500:])

def verify(fp, outdir, rows, lyrics, T, min_seg, max_seg):
    segs = [(r["start"], r["end"]) for r in rows]
    problems = []
    measured = []
    for r in rows:
        f = os.path.join(outdir, r["file"])
        d = probe_duration(fp, f); measured.append(d)
        if not (min_seg - 0.15 <= d <= max_seg + 0.15):
            problems.append(f"seg{r['seg']} 实测 {d:.3f}s 越界[{min_seg},{max_seg}]")
    split = [i + 1 for i, (s, e) in enumerate(sorted((s, e) for s, e, _ in lyrics))
             if not any(a <= s + 1e-6 and e <= b + 1e-6 for a, b in segs)]
    if split: problems.append("被切开的唱句: " + ",".join(map(str, split)))
    incut = [round(r["end"], 3) for r in rows[:-1] if r["cut"]]
    if incut: problems.append("切点进入唱词内部: " + ",".join(map(str, incut)))
    return dict(measured=[round(x, 3) for x in measured],
                total=round(sum(measured), 3), problems=problems, split=split)

def main():
    ap = argparse.ArgumentParser(description="歌词感知音乐分段（切点不进唱词，每段默认10-15s）")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--srt", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--analysis", help="analyze_audio.py 产出的 JSON")
    ap.add_argument("--bpm", type=float, help="手动锁定 BPM（慢板常需在半速/加倍间定夺）")
    ap.add_argument("--first-beat", type=float, dest="first_beat", help="首拍时间(秒)")
    ap.add_argument("--min", dest="min_seg", type=float, default=10.0)
    ap.add_argument("--max", dest="max_seg", type=float, default=15.0)
    ap.add_argument("--target", dest="target", type=float, default=12.5)
    ap.add_argument("--guard", type=float, default=0.06)
    ap.add_argument("--gap-step", dest="gap_step", type=float, default=0.02)
    ap.add_argument("--bitrate", default="192k")
    ap.add_argument("--plan", action="store_true", help="只输出方案与校验，不切割")
    ap.add_argument("--zip", dest="zip_path", help="切割后打包到该 zip 路径")
    ap.add_argument("--title", help="用于自动命名 zip（与 --zip 二选一）")
    args = ap.parse_args()

    for p, label in ((args.audio, "音频"), (args.srt, "SRT")):
        if not os.path.exists(p): sys.exit(f"找不到{label}：{p}")

    lyrics = parse_srt(args.srt)
    ff, fp = find_exe("ffmpeg"), find_exe("ffprobe")
    if not ff or not fp:
        sys.exit("未找到 ffmpeg/ffprobe，请安装并加入 PATH。")
    T = probe_duration(fp, args.audio)

    info = None
    if args.analysis:
        with open(args.analysis, "r", encoding="utf-8") as f:
            info = json.load(f)
    if args.bpm is None or args.first_beat is None:
        if info is None:
            try:
                from analyze_audio import analyze
                print("未提供 BPM，自动分析中…")
                info = analyze(args.audio)
                print(f"  自动 BPM {info['bpm']:.2f}（候选 {info['bpm_candidates']}，半/倍速 {info['tested_half_double']}）")
            except Exception as ex:
                print(f"  自动分析失败({ex})，回退 BPM 120/首拍0。建议用 --bpm/--first-beat 指定。")
    bpm = args.bpm if args.bpm is not None else (info["bpm"] if info else 120.0)
    fb = args.first_beat if args.first_beat is not None else (info["first_downbeat_sec"] if info else 0.0)

    cu = Cutter(lyrics, T, bpm, fb, args.min_seg, args.max_seg, args.target,
                args.guard, args.gap_step)
    rows = cu.plan()
    print(f"\n总时长 {T:.3f}s | BPM {bpm:.2f} beat {cu.beat:.3f}s bar {cu.bar:.3f}s 首拍 {fb:.3f}s "
          f"| 唱句 {len(lyrics)} 合并演唱区 {len(cu.OCC)} | 共 {len(rows)} 段\n")
    for r in rows:
        flag = "  <==切点进入唱句内部!" if r["cut"] else ""
        print(f"{r['seg']:>2} {r['start']:>8.3f} {r['end']:>8.3f} {r['length']:>6.3f}{flag}")
    in_range = all(args.min_seg - EPS <= r["length"] <= args.max_seg + EPS for r in rows)
    print(f"\n全部∈[{args.min_seg:g},{args.max_seg:g}]={in_range}; "
          f"进入唱词的切点={sum(r['cut'] for r in rows)}; 合计 {sum(r['length'] for r in rows):.3f}/原 {T:.3f}")
    bd = [0] + [r["end"] for r in rows]
    split = [i+1 for i, (s, e) in enumerate(cu.IV)
             if not any(a <= s + 1e-6 and e <= b + 1e-6 for a, b in zip(bd, bd[1:]))]
    print("被切开的唱句:", split if split else f"无 —— {len(cu.IV)} 句全部完整落在单段内")
    if args.plan:
        return

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("seg*.mp3"): old.unlink()
    segs = []
    for r in rows:
        a, b = r["start"], r["end"]
        nm = f"seg{r['seg']:02d}_{hms(a)}-{hms(b)}.mp3"
        r["file"] = nm
        cut_one(ff, args.audio, a, b, outdir / nm, args.bitrate)
        ev = [{"text": tx, "local_start": round(s - a, 3), "local_end": round(e - a, 3),
               "global_start": round(s, 3), "global_end": round(e, 3)}
              for s, e, tx in lyrics if a - 1e-6 <= s and e <= b + 1e-6]
        segs.append({**r,
                     "lyrics": [x["text"] for x in ev],
                     "lyric_events": ev,
                     "beats": cu.beats_in_segment(a, b)})
        print("已生成", nm)

    meta = {"source": os.path.basename(args.audio), "srt": os.path.basename(args.srt),
            "bpm": bpm, "beat_sec": round(cu.beat, 4), "bar_sec": round(cu.bar, 4),
            "phrase_sec": round(8 * cu.beat, 4), "first_downbeat_sec": fb,
            "min_sec": args.min_seg, "max_sec": args.max_seg, "target_sec": args.target,
            "total_duration": round(T, 3), "segment_count": len(segs),
            "accent_legend": {
                "phrase": f"乐句强拍 k%8==0（每8拍/2小节，约{8*cu.beat:.2f}s）",
                "downbeat": f"小节强拍 k%4==0（约{4*cu.beat:.2f}s）",
                "onbeat": f"正拍 k%2==0（约{2*cu.beat:.2f}s）",
                "offbeat": f"弱拍/反拍（每{cu.beat:.2f}s）"},
            "note": "beats[].local 为相对段头秒(H3段内用)，global 为原曲时间；散板/自由句以 lyric_events 字位为准。"}
    manifest = {"meta": meta, "segments": segs}
    (outdir / "cuts_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

    rep = verify(fp, outdir, rows, lyrics, T, args.min_seg, args.max_seg)
    print("\n=== ffprobe 独立校验 ===")
    print("实测段长:", rep["measured"])
    print(f"求和 {rep['total']:.3f}/原 {T:.3f}")
    if rep["problems"]:
        print("发现问题:")
        for x in rep["problems"]: print("  -", x)
    else:
        print("全部通过：无越界、无切词、唱句完整。")

    zip_path = args.zip_path
    if not zip_path and args.title:
        zip_path = str(outdir.parent / f"{args.title}_H3分段_{len(segs)}段_不切歌词.zip")
    if zip_path:
        files = sorted([p for p in outdir.iterdir()
                        if p.suffix.lower() == ".mp3" or p.name == "cuts_manifest.json"])
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files: z.write(p, p.name)
        print("已打包", zip_path, f"({len(files)} 个文件)")

    sys.exit(1 if rep["problems"] else 0)

if __name__ == "__main__":
    main()
