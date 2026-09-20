#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcribe_lyrics.py — 没有逐句歌词 SRT 时，用 mediakit-cli 的 ASR 从音频识别歌词，生成标准 SRT 草稿。

链路：mediakit-cli video asr-subtitles（云端，支持本地音频自动上传，歌曲用 singing 模式）
      → 轮询终态 → result.subtitles(start_time/end_time/subtitle_text/confidence)
      → 写标准 SubRip SRT（供 cut_by_lyrics.py 使用）+ 一份 .asr.json 原始结果。

用法：
  python transcribe_lyrics.py --audio "song.mp3"
  python transcribe_lyrics.py --audio "song.mp3" --language zh --content-type singing --out "song.srt"
  # 语种：auto（默认，自动探测）/ zh（简体中文 cmn-Hans-CN）/ en（英语 eng-US）
  # 内容：singing（默认，唱歌）/ speech（念白说话）/ auto（算法自动探测）

重要：唱歌（尤其古风/戏曲/慢板/强伴奏/和声）ASR 文字错误率高，间奏可能误识别出“啊”等拟声。
本脚本产出的是【草稿 SRT】，必须人工校对后再交给 cut_by_lyrics.py：
  1) 对照原词逐句改错字；2) 删掉间奏里的误识别短句；3) 核对每句起止边界与真实句读。
“不切词”依赖时间轴准确，未校对的 SRT 不得直接用于切割。
强伴奏识别过差时，可先用 mediakit-cli audio separate-voice 分离人声再识别（见 skill 文档）。
"""
import argparse, json, os, shutil, subprocess, sys

# 可选：PATH 找不到 mediakit-cli 时追加绝对路径兜底（列表可留空）
MEDIAKIT_FALLBACK = []
LANG_MAP = {"zh": "cmn-Hans-CN", "cn": "cmn-Hans-CN", "cmn": "cmn-Hans-CN",
            "en": "eng-US", "eng": "eng-US", "auto": None}
CONTENT_MAP = {"singing": "singing", "speech": "speech", "auto": None}


def find_cli(name):
    p = shutil.which(name)
    if p:
        return p
    for c in MEDIAKIT_FALLBACK:
        if os.path.exists(c):
            return c
    return None


def parse_json(txt):
    try:
        return json.loads(txt)
    except Exception:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            return json.loads(txt[i:j + 1])
        raise RuntimeError("无法解析 CLI 返回为 JSON：\n" + txt[-800:])


def run(cli, args, timeout=600):
    r = subprocess.run([cli] + args, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    if r.returncode != 0:
        raise RuntimeError(out[-1000:])
    return parse_json(out)


def srt_t(t):
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60)
    s = int(t % 60); ms = int(round((t - int(t)) * 1000))
    if ms == 1000:  # 四舍五入进位
        ms = 0; s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe(cli, audio, language="auto", content="singing", confidence=True,
               poll_interval=5.0):
    submit = ["video", "asr-subtitles", "--audio-url", os.path.abspath(audio)]
    lang = LANG_MAP.get(language.lower())
    if lang:
        submit += ["--language", lang]
    ct = CONTENT_MAP.get(content.lower(), None)
    if ct:
        submit += ["--content-type", ct]
    if confidence:
        submit += ["--enable-confidence=true"]
    print("提交 ASR 任务…（本地文件由 CLI 自动上传）")
    resp = run(cli, submit)
    task_id = resp.get("task_id")
    if not task_id:
        raise RuntimeError("未返回 task_id：" + json.dumps(resp, ensure_ascii=False))
    print("task_id =", task_id, "，轮询至完成…")
    res = run(cli, ["shared", "query-task", "--task-id", task_id,
                    "--poll-complete", "--poll-interval-seconds", str(poll_interval)],
              timeout=900)
    status = res.get("status")
    subs = res.get("subtitles")
    if not subs and isinstance(res.get("result"), dict):
        subs = res["result"].get("subtitles")
    if status not in (None, "completed", "succeeded", "success") or not subs:
        raise RuntimeError("ASR 未成功完成：" + json.dumps(res, ensure_ascii=False)[:1000])
    subs = sorted(subs, key=lambda x: x.get("start_time", 0.0))
    return subs, res


def to_srt(subs):
    lines = []
    for i, s in enumerate(subs, 1):
        st, et = s.get("start_time", 0.0), s.get("end_time", 0.0)
        text = (s.get("subtitle_text") or "").strip()
        lines.append(str(i))
        lines.append(f"{srt_t(st)} --> {srt_t(et)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main():
    ap = argparse.ArgumentParser(description="ASR 识别歌词，生成供切割使用的 SRT 草稿（必须人工校对）")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", help="输出 SRT 路径（默认与音频同名 .srt）")
    ap.add_argument("--language", default="auto", help="auto | zh | en")
    ap.add_argument("--content-type", dest="content", default="singing",
                    help="singing（默认）| speech | auto")
    ap.add_argument("--no-confidence", action="store_true", help="不返回置信度")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        sys.exit(f"找不到音频：{args.audio}")
    cli = find_cli("mediakit-cli")
    if not cli:
        sys.exit("未找到 mediakit-cli（需在装有 MediaKit 的豆包环境运行，或把其路径加入 MEDIAKIT_FALLBACK）。")

    subs, raw = transcribe(cli, args.audio, args.language, args.content,
                           confidence=not args.no_confidence,
                           poll_interval=args.poll_interval)
    out_srt = args.out or (os.path.splitext(args.audio)[0] + ".srt")
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write(to_srt(subs))
    raw_path = os.path.splitext(out_srt)[0] + ".asr.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    confs = [s.get("confidence") for s in subs if s.get("confidence") is not None]
    print(f"\n识别 {len(subs)} 句 -> {out_srt}")
    print("原始结果(含置信度) ->", raw_path)
    if confs:
        avg = sum(confs) / len(confs)
        low = [(i + 1, round(c, 2), s.get("subtitle_text", ""))
               for i, (c, s) in enumerate(zip(confs, subs)) if c < 0.5]
        print(f"平均置信度 {avg:.2f}；低置信(<0.5) {len(low)} 句")
        for n, c, t in low:
            print(f"  #{n:<2} {c}  {t}")
    print("\n注意：这是草稿 SRT。请逐句校对歌词文字、删除间奏误识别、核对起止边界后，")
    print("      再运行 cut_by_lyrics.py --srt 该文件 进行切割。")


if __name__ == "__main__":
    main()
