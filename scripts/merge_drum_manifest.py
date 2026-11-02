# -*- coding: utf-8 -*-
"""把反推得到的 drum_events 并入 cuts_manifest，输出完整 JSON（含全局与段内视图）。"""
import json

base = r"C:\Users\Administrator\Doubao\chats\2026-11-01\new-chat"
man_p = base + r"\おはよう！禁書目録_H3分段_18段_不切歌词\cuts_manifest.json"
drum_p = base + r"\drum_events.json"
out_p = base + r"\cuts_manifest_with_drum_events.json"

man = json.load(open(man_p, encoding="utf-8"))
drum = json.load(open(drum_p, encoding="utf-8"))
dmeta, events = drum["detection_meta"], drum["drum_events"]

# 顶层：全局事件 + 检测元数据
man["drum_detection"] = dmeta
man["drum_events"] = events

# 每段：段内 local 视图
segs = man["segments"]
nseg = len(segs)
assigned = 0
for i, seg in enumerate(segs):
    s0, s1 = float(seg["start"]), float(seg["end"])
    last = (i == nseg - 1)
    local_events = []
    for e in events:
        t = e["time"]
        inside = (s0 <= t < s1) or (last and t == s1)
        if inside:
            ee = dict(e)
            ee["local"] = round(t - s0, 4)
            local_events.append(ee)
    seg["drum_events"] = local_events
    assigned += len(local_events)

# 校验
assert assigned == len(events), "事件归属不一致 %d vs %d" % (assigned, len(events))
# 段内时间范围校验
for seg in segs:
    for e in seg["drum_events"]:
        assert 0 <= e["local"] <= float(seg["length"]) + 1e-6
# 全局时间单调
tg = [e["time"] for e in events]
assert all(tg[i] <= tg[i+1] for i in range(len(tg)-1))

json.dump(man, open(out_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("输出:", out_p)
print("顶层键:", list(man.keys()))
print("段数 %d, 全局事件 %d, 段内归属合计 %d (一致)" % (nseg, len(events), assigned))
print("各段事件数:", [len(s["drum_events"]) for s in segs])
print("段内事件示例:", json.dumps(segs[2]["drum_events"][0], ensure_ascii=False))
