#!/usr/bin/env python3
"""把 Chrome 插件采集的内容从下载目录收进工作台。

用法:  python3 scripts/ingest_inbox.py          收进来并列出待拆解的
       python3 scripts/ingest_inbox.py --keep   保留下载目录里的原件

插件写到 ~/Downloads/xhs-inbox/，按内容分两路：
  笔记      → local/data/inbox/，按 noteId 去重，同一篇重复采集只保留最新的一份
  账号主页  → local/data/benchmark/{userId}.json，并重算对标汇总
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark  # noqa: E402
import trash  # noqa: E402
from paths import DOWNLOAD_INBOX as SRC, INBOX as DST  # noqa: E402


def note_id(payload, path):
    """noteId 缺失时退回用链接或文件名，保证去重有个稳定依据。"""
    return payload.get("noteId") or payload.get("url") or path.stem


def local_date(iso):
    """插件记的是 UTC 时间，换成本地日期，免得晚上采的算成前一天。"""
    try:
        return datetime.fromisoformat(iso).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return None


def ingest_profile(payload):
    """账号主页 → 对标数据。返回 (昵称, 新增条数)；数据不全或账号已删除时返回 None。

    工作台里删掉的账号，插件批量更新再交来时不收；手动抓取主页视为重新加入。
    """
    uid, notes = payload.get("userId"), payload.get("notes") or []
    if not uid or not notes:
        return None
    if uid in trash.removed_accounts():
        if payload.get("batch"):
            return None
        trash.set_removed(uid, False)
    new = benchmark.new_notes(uid, notes)
    keep = {k: v for k, v in payload.items() if k not in ("kind", "warnings", "batch")}
    keep["notes"] = benchmark.merge_notes(uid, notes)
    benchmark.save_account(uid, {**keep, "via": "插件"},
                           fetched=local_date(payload.get("capturedAt")))
    return payload.get("nickname") or uid, len(new)


def main():
    keep = "--keep" in sys.argv
    if not SRC.exists():
        print(f"下载目录还没有内容：{SRC}")
        print("在小红书笔记页或账号主页点插件的「抓取」按钮就会出现。")
        return

    DST.mkdir(parents=True, exist_ok=True)

    # 已收过的：noteId -> 文件路径
    seen = {}
    for f in DST.glob("*.json"):
        try:
            seen[note_id(json.loads(f.read_text(encoding="utf-8")), f)] = f
        except (json.JSONDecodeError, OSError):
            continue

    added, replaced, profiles, skipped = [], [], [], 0
    for f in sorted(SRC.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  跳过（不是合法 JSON）：{f.name}")
            skipped += 1
            continue

        if payload.get("kind") == "profile":
            got = ingest_profile(payload)
            if got:
                profiles.append(got)
            else:
                why = "已在工作台删除的账号" if payload.get("userId") in trash.removed_accounts() else "主页数据不全"
                print(f"  跳过（{why}）：{f.name}")
                skipped += 1
            if not keep:
                f.unlink(missing_ok=True)
            continue

        nid = note_id(payload, f)
        target = DST / f.name
        old = seen.get(nid)

        if old and old.name != f.name:
            old.unlink(missing_ok=True)
            replaced.append(payload.get("title") or f.name)
        elif not old:
            added.append(payload.get("title") or f.name)

        shutil.copy2(f, target)
        seen[nid] = target
        if not keep:
            f.unlink(missing_ok=True)

    if profiles:
        summary = benchmark.write_summary(sum(n for _, n in profiles))
        print(f"✓ 对标数据 {len(summary['accounts'])} 个账号 · {summary['total']} 条笔记")
        for nick, n in profiles:
            print(f"  ↻ {nick}：新增 {n} 条")

    total = len(list(DST.glob("*.json")))
    print(f"✓ 收件箱 {DST}")
    print(f"  新增 {len(added)} 篇 · 更新 {len(replaced)} 篇 · 跳过 {skipped} · 累计 {total} 篇")
    for t in added:
        print(f"  + {t}")
    for t in replaced:
        print(f"  ↻ {t}")

    videos = []
    for f in DST.glob("*.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if p.get("type") == "视频":
            videos.append(p.get("title") or f.stem)
    if videos:
        print(f"\n  其中 {len(videos)} 篇是视频笔记，口播文案要另走转写：")
        for t in videos:
            print(f"    · {t}")
        print("  转写：python3 scripts/transcribe_inbox.py（生成工作台时也会自动起）")

    if total:
        print("\n在工作台的采集箱里选中笔记即可分析。")


if __name__ == "__main__":
    main()
