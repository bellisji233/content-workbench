#!/usr/bin/env python3
"""把工作台里跑出的爆款分析合并进资产总库。

用法:  python3 scripts/merge_assets.py           合并下载目录里的分析
       python3 scripts/merge_assets.py --dry     只看会写什么，不落盘

流程：工作台点「导出到资产库」→ 文件落到 ~/Downloads/xhs-analyses/ → 跑本脚本。
分析里的「可迁移资产」和「共性框架」按分类追加进总库，来源标明出自哪篇笔记。

总库是写稿时会读的那个文件，所以这一步是采集闭环的最后一环：
不跑这步，分析结果就停在工作台里，进不了下一篇稿子。
"""

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ANALYSES as ARCHIVE, DOWNLOAD_ANALYSES as SRC, MINING, VAULT  # noqa: E402

TODAY = date.today().isoformat()

# 分析里的小节 → 总库里的分类。对不上的丢进「论证/结构」，不新建分类。
SECTION_MAP = {
    "开头钩子": "开场/钩子", "共性钩子公式": "开场/钩子",
    "选题切口": "选题", "共性选题公式": "选题",
    "结构骨架": "论证/结构", "共性结构骨架": "论证/结构",
    "论证链路": "论证/结构", "共性论证路径": "论证/结构",
    "语言话术": "话术/金句",
    "情绪节奏与收尾": "收尾/CTA",
}


def split_sections(md):
    out, cur, buf = {}, None, []
    for line in md.splitlines():
        m = re.match(r"^#{2,3}\s+(?:\d+\.\s*)?(.+)$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip(), []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def bullets(text):
    """抽要点行，去掉编号和加粗标记，太短的丢掉。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not re.match(r"^[-*·]|\d+[.、)]", line):
            continue
        line = re.sub(r"^[-*·]\s*|^\d+[.、)]\s*", "", line).strip()
        line = line.replace("**", "").strip()
        if len(line) >= 8:
            out.append(line)
    return out


def extract(analysis):
    """一份分析 → [(分类, 资产描述)]。优先取「可迁移资产」，那是作者已经提炼过的。

    选题推荐是选题层面的产出，不是可复用的内容零件，不并进资产库。
    """
    if analysis.get("kind") == "选题推荐":
        return [], analysis.get("title", "")
    secs = split_sections(analysis.get("markdown", ""))
    title = analysis.get("title", "未命名")
    rows = []

    for key in ("可迁移资产", "可迁移清单"):
        if key in secs:
            for b in bullets(secs[key]):
                rows.append(("论证/结构", b))

    for name, cat in SECTION_MAP.items():
        if name in secs:
            for b in bullets(secs[name])[:3]:
                rows.append((cat, b))

    # 同一条资产可能在两节里都出现，按文本去重
    seen, uniq = set(), []
    for cat, text in rows:
        k = text[:40]
        if k in seen:
            continue
        seen.add(k)
        uniq.append((cat, text))
    return uniq, title


def ensure_vault():
    """没接拆解项目时，爆款库是本地的一个文件；第一次保存时按分类建好空表。"""
    if VAULT.exists() or MINING:
        return
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    cats = dict.fromkeys(SECTION_MAP.values())
    VAULT.write_text("# 爆款库\n\n" + "".join(
        f"## {c}\n\n| 资产 | 来源 | 使用记录 | 评级 |\n|---|---|---|---|\n\n" for c in cats),
        encoding="utf-8")


def append_to_vault(by_cat, dry=False):
    ensure_vault()
    if not VAULT.exists():
        print(f"找不到资产总库：{VAULT}")
        return 0
    lines = VAULT.read_text(encoding="utf-8").splitlines()
    added = 0

    for cat, rows in by_cat.items():
        # 定位分类标题，插在该分类表格的最后一行之后
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == f"## {cat}")
        except StopIteration:
            print(f"  分类「{cat}」在总库里不存在，跳过 {len(rows)} 条")
            continue
        end = start + 1
        last_row = None
        while end < len(lines) and not lines[end].startswith("## "):
            if lines[end].strip().startswith("|"):
                last_row = end
            end += 1
        if last_row is None:
            print(f"  分类「{cat}」下没有表格，跳过")
            continue

        existing = "\n".join(lines[start:end])
        new = []
        for text, src in rows:
            if text[:24] in existing:          # 已经有了就不重复追加
                continue
            new.append(f"| {text} | 采集分析 · {src} | 未回流 | 待验证 |")
        if new:
            lines[last_row + 1:last_row + 1] = new
            added += len(new)
            print(f"  {cat}：+{len(new)} 条")

    if added and not dry:
        VAULT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def main():
    dry = "--dry" in sys.argv
    files = sorted(SRC.glob("*.json")) if SRC.exists() else []
    if not files:
        print(f"没有待合并的分析：{SRC}")
        print("在工作台的采集箱里跑完分析，点「导出到资产库」即可。")
        return

    by_cat = {}
    titles = []
    for f in files:
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  跳过（不是合法 JSON）：{f.name}")
            continue
        rows, title = extract(a)
        titles.append(title)
        for cat, text in rows:
            by_cat.setdefault(cat, []).append((text, title))

    print(f"待合并 {len(files)} 份分析，抽出 {sum(len(v) for v in by_cat.values())} 条资产：")
    added = append_to_vault(by_cat, dry)

    if dry:
        print(f"\n[dry-run] 会追加 {added} 条，未写入。")
        return

    if added:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.move(str(f), ARCHIVE / f"{TODAY}-{f.name}")
        print(f"\n✓ 已追加 {added} 条到资产总库")
        print(f"  原始分析归档到 {ARCHIVE}")
        print("  新条目都标为「待验证」，发布后回填表现数据才会升级")
    else:
        print("\n没有新增（可能都已经在总库里了）")


def merge_record(rec):
    """一份分析直接并入资产总库并归档原文，返回新增条数。

    本地工作台点「保存分析」走这里，不经过下载目录。
    """
    import contextlib
    import io
    rows, title = extract(rec)
    by_cat = {}
    for cat, text in rows:
        by_cat.setdefault(cat, []).append((text, title))
    with contextlib.redirect_stdout(io.StringIO()):
        added = append_to_vault(by_cat)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", title)[:40] or "分析"
    (ARCHIVE / f"{date.today().isoformat()}-{name}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def auto_merge():
    """供 build_workbench 调用：静默合并，返回新增条数。"""
    if not SRC.exists() or not any(SRC.glob("*.json")):
        return 0
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main()
    m = re.search(r"已追加 (\d+) 条", buf.getvalue())
    return int(m.group(1)) if m else 0


if __name__ == "__main__":
    main()
