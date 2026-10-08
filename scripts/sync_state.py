#!/usr/bin/env python3
"""本地工作台与线上工作台之间，同步状态改动和分析记录。

本地存在 local/data/state/{status,analyses}/，线上存在工作台页面的数据库里，结构一致。
线上数据库只有 agent 能读写，所以同步分三步，由 agent 按 SKILL.md「同步工作台」执行：

  1. agent 把线上两个集合导出到 local/data/state/_online/{collection}/{id}.json
  2. python3 scripts/sync_state.py pull
       线上较新或本地没有的 → 写进本地
       本地较新或线上没有的 → 列进 local/data/state/_push.json，交给 agent 写到线上
  3. agent 推送完成后：python3 scripts/sync_state.py mark
       记下这次同步后两边都有的条目。下次某条只剩一边，就能分清是另一边删了，还是还没同步过

新旧按文档里的 updatedAt / createdAt 比，本地删除留有带 _savedAt 的删除记录。
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import STATE  # noqa: E402

COLLECTIONS = ("status", "analyses")
ONLINE = STATE / "_online"
PUSH = STATE / "_push.json"
SYNCED = STATE / "_synced.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_dir(d):
    out = {}
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def clean(doc):
    """推到线上时去掉本地记账用的字段。"""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def stamp(doc):
    return doc.get("updatedAt") or doc.get("createdAt") or doc.get("_savedAt") or ""


def write_local(coll, doc_id, doc):
    p = STATE / coll / f"{doc_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def pull():
    if not ONLINE.exists():
        raise SystemExit(f"没有线上导出：{ONLINE}（先按 SKILL.md 导出两个集合）")
    synced = json.loads(SYNCED.read_text(encoding="utf-8")) if SYNCED.exists() else {}
    last_sync = synced.get("at", "")
    writes, expected, report = [], {}, []

    for coll in COLLECTIONS:
        online = read_dir(ONLINE / coll)
        local = read_dir(STATE / coll)
        was_synced = set(synced.get(coll, []))
        expect = set(online)
        got_in = got_out = dropped = 0

        for doc_id in sorted(set(online) | set(local)):
            on, lo = online.get(doc_id), local.get(doc_id)
            deleted = bool(lo and lo.get("_deleted"))

            if on and not lo:                                   # 线上新增
                write_local(coll, doc_id, on)
                got_in += 1
            elif lo and not deleted and not on:
                if doc_id in was_synced and lo.get("_savedAt", "") <= last_sync:
                    write_local(coll, doc_id, {"_deleted": True, "_savedAt": now_iso()})
                    dropped += 1                                # 线上删了，本地没再改过
                else:
                    writes.append({"op": "set", "collection": coll, "doc_id": doc_id,
                                   "data": clean(lo)})
                    expect.add(doc_id)
                    got_out += 1
            elif deleted and on:
                if lo["_savedAt"] > stamp(on):                  # 本地删在后
                    writes.append({"op": "delete", "collection": coll, "doc_id": doc_id})
                    expect.discard(doc_id)
                    got_out += 1
                else:
                    write_local(coll, doc_id, on)
                    got_in += 1
            elif on and lo and not deleted:
                if stamp(lo) > stamp(on):
                    writes.append({"op": "set", "collection": coll, "doc_id": doc_id,
                                   "data": clean(lo)})
                    got_out += 1
                elif stamp(on) > stamp(lo):
                    write_local(coll, doc_id, on)
                    got_in += 1

        expected[coll] = sorted(expect)
        report.append(f"  {coll}：线上 → 本地 {got_in} 条，本地 → 线上 {got_out} 条"
                      + (f"，线上已删除 {dropped} 条" if dropped else ""))

    PUSH.write_text(json.dumps({"writes": writes, "expected": expected},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(ONLINE)        # 导出用完即删，下次导出时不会混进已在线上删除的条目
    print("✓ 已合并线上改动")
    print("\n".join(report))
    if writes:
        print(f"  待推送 {len(writes)} 条：{PUSH}（已在线上的条目推送时要带它的 version）")
    else:
        print("  没有需要推送的改动，可以直接 mark")


def mark():
    if not PUSH.exists():
        raise SystemExit("先运行 pull")
    plan = json.loads(PUSH.read_text(encoding="utf-8"))
    SYNCED.write_text(json.dumps({"at": now_iso(), **plan["expected"]},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    PUSH.unlink()
    print(f"✓ 已记录同步点：{SYNCED}")


if __name__ == "__main__":
    {"pull": pull, "mark": mark}.get(sys.argv[1] if len(sys.argv) > 1 else "",
                                     lambda: print(__doc__))()
