#!/usr/bin/env python3
"""接入向导用的检查工具。agent 按 SKILL.md「建工作台」带用户走一遍，这里负责验证。

用法（在工作台目录下运行）:
  python3 scripts/onboard.py init      没有 local/ 时，从 local.example/ 复制一份
  python3 scripts/onboard.py check     逐项检查配置：路径、平台导出、稿件、流水线、分类
  python3 scripts/onboard.py titles    列出全部发布标题和当前归类，供起草选题分类
  python3 scripts/onboard.py scan 目录  扫一遍生产流水线文件夹，列出每期常见的文件，供起草 [pipeline]
"""

import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
WB = HERE.parent


def init():
    import os
    local = Path(os.environ.get("WORKBENCH_LOCAL") or WB / "local")
    if (local / "config.toml").exists():
        print(f"已有配置：{local / 'config.toml'}，不覆盖")
        return
    shutil.copytree(WB / "local.example", local, dirs_exist_ok=True)
    print(f"✓ 已创建 {local}，接下来改 config.toml")


def check():
    sys.path.insert(0, str(HERE))
    import paths
    import build_workbench as bw

    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print(("  ✓ " if good else "  ✗ ") + text)

    print(f"配置：{paths.LOCAL / 'config.toml'}")
    if not (paths.LOCAL / "config.toml").exists():
        print("  ✗ 没有配置文件，先运行 init")
        sys.exit(1)

    print("平台数据（创作者后台导出的 xlsx 放进各平台的文件夹，可再按日期分子文件夹）")
    for pf in bw.PLATFORMS:
        folder = paths.EXPORTS / pf["dir"]
        if not folder.exists():
            folder.mkdir(parents=True)
            print(f"  · 已创建 {folder}")
        rows, path = bw.load_platform(pf)
        if path:
            line(True, f"{pf['name']}：{path.relative_to(paths.EXPORTS)}，{len(rows)} 条")
        else:
            line(False, f"{pf['name']}：{folder} 里还没有表头含「{pf['columns']['title']}」的 xlsx")

    print("稿件")
    if paths.DRAFTS:
        n = len(bw.scan_drafts())
        line(paths.DRAFTS.exists(), f"{paths.DRAFTS}，{n} 篇")
    else:
        print("  · 未配置稿件文件夹")

    print("生产流水线")
    if paths.WORKS:
        n = len(bw.scan_works())
        line(paths.WORKS.exists(), f"{paths.WORKS}，{n} 期 · 阶段 {' → '.join(l for _, l in bw.STAGES)}")
    else:
        print("  · 未配置，阶段为 " + " → ".join(l for _, l in bw.STAGES))

    print("稿件和发布的关联（复盘爆款库靠它知道每条作品用了什么）")
    items, pubs, orphans, *_ = bw.build()
    if pubs:
        linked = len(pubs) - len(orphans)
        line(linked > 0, f"平台上 {len(pubs)} 条发布，{linked} 条找到了对应稿件（{linked * 100 // len(pubs)}%）")
        recent = sorted(orphans, key=lambda o: str(o.get("date") or ""), reverse=True)[:8]
        if recent:
            print("  · 没找到稿件的发布（最近的几条）：")
            for o in recent:
                print(f"      {o.get('date') or '—'}  {o['platform']}  {o['title'][:40]}")
            print("    稿件在但标题不同：在 local/status.md「发布关联」写一行 key → 平台标题片段；"
                  "\n    本来就没有稿件的旧作品：写进「不再补稿」")
    else:
        print("  · 还没有平台数据，放入导出表格后再检查")

    print("选题分类")
    from classify import RULES
    line(bool(RULES), f"{len(RULES)} 类" if RULES else "还没有分类，全部会归入「其他」（运行 titles 起草）")

    print("其他")
    print(f"  · 账号：{paths.CONFIG.get('account', {}).get('name') or '未填写（选题推荐会缺少账号背景）'}")
    print(f"  · 爆款库：{paths.VAULT}{'' if paths.VAULT.exists() else '（第一次保存拆解时自动创建）'}")
    print(f"  · 线上工作台：{paths.WORKBENCH_URL or '未配置'}")
    print("\n" + ("✓ 可以打开工作台：python3 scripts/serve.py" if ok else "✗ 先处理上面打叉的项"))


# 代码和构建产物不算阶段产出，扩展名分组时跳过
CODE_EXT = {".py", ".pyc", ".js", ".mjs", ".cjs", ".ts", ".css", ".json", ".sh", ".map",
            ".lock", ".log", ".woff", ".woff2", ".ttf", ".otf"}


def scan(target):
    """列出流水线文件夹里每期常见的文件和子文件夹，按通常的产生先后排。

    只统计，不写配置：agent 拿结果和用户确认「哪些文件出现算完成哪个阶段」后再写 [pipeline]。
    """
    root = Path(target).expanduser()
    eps = [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")] \
        if root.is_dir() else []
    if not eps:
        print(f"{root} 下没有子文件夹")
        return
    dated = sum(bool(re.match(r"\d{4}-\d{2}-\d{2}-", d.name)) for d in eps)
    print(f"{root}：{len(eps)} 个子文件夹，其中 {dated} 个按「日期-标题」命名")
    if dated < len(eps):
        print("  未按「日期-标题」命名的文件夹，标题取文件夹名，没有日期")

    seen, pos = Counter(), defaultdict(list)
    for d in eps:
        first = {}
        for f in d.rglob("*"):
            rel = f.relative_to(d)
            if any(x.startswith(".") for x in rel.parts) or not f.is_file():
                continue
            # 顶层文件按文件名；子文件夹里的只记到「子文件夹/」和「*.扩展名」
            keys = [rel.name] if len(rel.parts) == 1 else [rel.parts[0] + "/"]
            if f.suffix and f.suffix.lower() not in CODE_EXT:
                keys.append("*" + f.suffix.lower())
            st = f.stat()
            mt = getattr(st, "st_birthtime", st.st_mtime)   # 有创建时间就用创建时间
            for k in keys:
                first[k] = min(first.get(k, mt), mt)
        ranked = sorted(first, key=first.get)
        for i, k in enumerate(ranked):
            seen[k] += 1
            pos[k].append(i / max(1, len(ranked) - 1))

    common = [k for k, n in seen.items() if n >= max(2, len(eps) * 0.2)]
    common.sort(key=lambda k: statistics.median(pos[k]))
    print(f"\n至少 20% 的期里出现的文件（按通常的产生先后排）：")
    print("出现期数\t先后\t文件")
    for k in common:
        print(f"{seen[k]}/{len(eps)}\t{statistics.median(pos[k]):.2f}\t{k}")
    print("\n先后：0 表示通常最早出现，1 表示最晚。按它把文件分成阶段，和用户确认后写进 [pipeline]。")


def titles():
    sys.path.insert(0, str(HERE))
    import build_workbench as bw
    from classify import classify
    seen, rows = set(), []
    for pf in bw.load_platforms():
        for r in pf["rows"]:
            if r["title"] not in seen:
                seen.add(r["title"])
                rows.append((classify(r["title"]), r["views"], r["fans"], r["title"][:60]))
    count = Counter(c for c, *_ in rows)
    print(f"共 {len(rows)} 个标题 · 当前归类：" + "、".join(f"{k} {v}" for k, v in count.most_common()))
    print("归类\t播放\t涨粉\t标题")
    for c, v, f, t in sorted(rows, key=lambda x: (x[0], -x[1])):
        print(f"{c}\t{v}\t{f}\t{t}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "scan" and len(sys.argv) > 2:
        scan(sys.argv[2])
    else:
        {"init": init, "check": check, "titles": titles}.get(cmd, lambda: print(__doc__))()
