#!/usr/bin/env python3
"""爆款库回流：把已发布作品的数据记到它用过的爆款资产上，按记录给资产评级。

工作台能算的部分在这里：账号基线、每条作品相对基线的倍数、稿件在哪、爆款库里有哪些资产。
「这篇稿子用了哪几条资产」要读稿判断，由 agent 按 SKILL.md「复盘爆款库」完成后用 record 写回。

用法（在工作台目录下运行）:
  python3 scripts/asset_retro.py list [--days 90]   待回流的作品 + 账号基线 + 爆款库资产
  python3 scripts/asset_retro.py record --asset 资产名 --work 作品标题 --verdict 优|平|弱 --note "依据"
  python3 scripts/asset_retro.py skip --work 作品标题 --note "原因"    复盘过但没用到库内资产，不再列为待回流
  python3 scripts/asset_retro.py prompt                    打印给模型的复盘提示词（工作台按钮用的同一份）
  python3 scripts/asset_retro.py apply plan.json           按模型给出的 JSON 批量写入
"""

import argparse
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_workbench as bw  # noqa: E402
from merge_assets import ensure_vault  # noqa: E402
from paths import VAULT, WORKS  # noqa: E402

GOOD, BAD = 1.3, 0.7     # 相对基线 ≥1.3 倍算明显好，≤0.7 倍算明显差（2s 跳出率反过来）

# 指标：(字段, 名称, 越高越好)
METRICS = [
    ("views", "播放", True),
    ("collect_rate", "收藏率", True),
    ("fans_per_k", "涨粉/千播", True),
    ("ctr", "封面点击率", True),
    ("finish5s", "5s完播", True),
    ("bounce2s", "2s跳出", False),
]


def metric(p, key):
    v = p.get("views") or 0
    if key == "views":
        return v or None
    if key == "collect_rate":
        return p["collects"] / v if v else None
    if key == "fans_per_k":
        return p["fans"] / v * 1000 if v else None
    return p.get(key) or None


def baselines(pubs):
    out = {}
    for pf in {p["platform"] for p in pubs}:
        rows = [p for p in pubs if p["platform"] == pf]
        out[pf] = {}
        for key, _, _ in METRICS:
            vals = [x for x in (metric(p, key) for p in rows) if x is not None]
            if len(vals) >= 3:
                out[pf][key] = statistics.median(vals)
    return out


def fmt(key, v):
    if key == "views":
        return f"{v:,.0f}"
    if key == "fans_per_k":
        return f"{v:.2f}‰"
    return f"{v * 100:.1f}%"


def script_location(item):
    if item["track"] == "手写":
        return [Path(item["path"]).expanduser()]
    folder = WORKS / item["key"] if WORKS else None
    if not folder or not folder.exists():
        return []
    return sorted(folder.glob("*.md"))


# ---------------------------------------------------------------- 爆款库表格

def vault_rows():
    """爆款库里的资产：[(行号, 分类, 单元格列表)]。表格是 | 资产 | 来源 | 使用记录 | 评级 |。"""
    if not VAULT.exists():
        return []
    out, cat = [], None
    for i, line in enumerate(VAULT.read_text(encoding="utf-8").splitlines()):
        if line.startswith("## "):
            cat = line[3:].strip()
            continue
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] == "资产":
            continue
        out.append((i, cat, cells))
    return out


def grade_of(record, current):
    """按使用记录里的【优/平/弱】给评级。

    只改「待验证 / ✅ / ⚠️」开头的评级；人设组件、归因结论、观察线索等人工评级原样保留。
    旧格式的人工小结（如「待验证（1次优）」）保留在括号里，其中的「N次优/平/弱」计入次数。
    """
    current = current.strip()
    if current and not current.startswith(("待验证", "✅", "⚠️")):
        return current
    good, flat, bad = (record.count(f"【{x}】") for x in ("优", "平", "弱"))
    legacy = ""
    m = re.match(r"待验证（(.*)）$", current)
    if m and not re.match(r"\d+ 次：", m.group(1)):
        legacy = m.group(1).split("；新增")[0]
    lg = {k: sum(int(n) for n in re.findall(rf"(\d+)\s*次{k}", legacy)) for k in ("优", "平", "弱")}
    g, b = good + lg["优"], bad + lg["弱"]
    if g >= 2 and g > b:
        return "✅优先"
    if b >= 2 and b > g:
        return "⚠️降级"
    if current.startswith(("✅", "⚠️")):          # 人工给过结论、新记录还不够推翻时保留
        return current
    n = good + flat + bad
    if legacy:
        return f"待验证（{legacy}；新增 优 {good} / 平 {flat} / 弱 {bad}）"
    return f"待验证（{n} 次：优 {good} / 平 {flat} / 弱 {bad}）" if n else "待验证"


# ---------------------------------------------------------------- 命令

RULES = (f"判定：相对基线 ≥{GOOD} 倍记「优」，≤{BAD} 倍记「弱」，其间记「平」（2s 跳出率反过来）。"
         "\n看哪个指标：口播开场看 5s完播、2s跳出（没有视频数据时看封面点击率）；标题/封面看封面点击率；"
         "选题看播放、涨粉/千播；结构与话术看收藏率；收尾/CTA 看涨粉/千播。"
         "\n几个平台里有优无弱记「优」，有弱无优记「弱」，其余记「平」。")


def pending(days=bw.RETRO_DAYS[1]):
    """(账号基线, 待回流作品列表)。作品带 scripts（稿件文件）和 first（首次发布日期）。"""
    items, pubs, *_ = bw.build()
    base = baselines(pubs)
    names = bw.recorded_names(VAULT.read_text(encoding="utf-8")) if VAULT.exists() else set()
    today = date.today()
    todo = []
    for w in bw.published_works(items):
        if bw.is_recorded(w["name"], names):
            continue
        dates = [p["date"] for p in w["pubs"] if p["date"]]
        w["first"] = min(dates) if dates else None
        if w["first"] and not bw.RETRO_DAYS[0] <= (today - w["first"]).days <= days:
            continue
        w["scripts"] = []
        for it in w["items"]:
            w["scripts"] += [x for x in script_location(it) if x not in w["scripts"]]
        todo.append(w)
    todo.sort(key=lambda w: w["first"] or date.min, reverse=True)
    return base, todo


def baseline_lines(base):
    return [f"- {pf}：" + " · ".join(f"{name} {fmt(k, b[k])}" for k, name, _ in METRICS if k in b)
            for pf, b in base.items()]


def metric_lines(w, base):
    out = []
    for p in w["pubs"]:
        b = base.get(p["platform"], {})
        parts = []
        for k, name, higher in METRICS:
            v = metric(p, k)
            if v is None or k not in b or not b[k]:
                continue
            r = v / b[k]
            tag = "" if BAD < r < GOOD else ("（好）" if (r >= GOOD) == higher else "（差）")
            parts.append(f"{name} {fmt(k, v)}，{r:.2f} 倍{tag}")
        out.append(f"- {p['platform']}：" + "；".join(parts))
    return out


def asset_lines():
    out, cat = [], None
    for _, c, cells in vault_rows():
        if c != cat:
            cat = c
            out.append(f"\n{c}")
        n = cells[2].count("《")
        out.append(f"- {cells[0]} ｜ {cells[3]}{f' ｜ 已有 {n} 条记录' if n else ''}")
    return out


# ---------------------------------------------------------------- 稿件节选

SIDE_FILES = re.compile(r"AGENTS|CLAUDE|DESIGN|SOURCES|titles|review|teardown|candidates|research|"
                        r"framework|评审|废弃|regression", re.I)


def section(text, title):
    """取标题含 title 的那一节（到下一个同级或更高级标题为止）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s*.*" + re.escape(title), line)
        if m:
            lvl = len(m.group(1))
            body = []
            for l2 in lines[i + 1:]:
                h = re.match(r"^(#+)\s", l2)
                if h and len(h.group(1)) <= lvl:
                    break
                body.append(l2)
            return "\n".join(body).strip()
    return ""


def main_script(paths):
    """一期里真正发出去的那份稿：02-draft.md > 带「口播」的 > 带 draft/script 的 > 其余最大的。"""
    cands = [p for p in paths if not SIDE_FILES.search(p.name)] or list(paths)
    for test in (lambda n: n == "02-draft.md", lambda n: "口播" in n,
                 lambda n: "draft" in n.lower() and "ai-final" not in n, lambda n: "script" in n.lower()):
        hit = [p for p in cands if test(p.name)]
        if hit:
            return hit[0]
    return max(cands, key=lambda p: p.stat().st_size) if cands else None


def excerpt(paths, head=1200, tail=400):
    """给模型看的稿件节选：写稿时留下的「资产使用记录」+ 正稿开头和结尾。"""
    out = []
    main = main_script(paths)
    # 正稿里的记录优先；旧版本稿件里的记录可能和最终发出去的不一致
    for p in ([main] if main else []) + [x for x in paths if x != main]:
        sec = section(p.read_text(encoding="utf-8", errors="ignore"), "资产使用记录")
        if sec:
            out.append(f"【写稿时记录的资产使用 · {p.name}】\n{sec[:800]}")
            break
    if main:
        text = main.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^#+\s*.*(口播稿|正稿|旁白).*$", text, re.M)   # 长文档里正稿常在某一节
        if m and m.start() > 0:
            text = text[m.end():]
        body = "\n".join(l for l in text.splitlines()
                         if l.strip() and not l.lstrip().startswith(("#", ">", "|", "![", "---")))
        out.append(f"【正稿 · {main.name}】\n" + (body if len(body) <= head + tail
                                                 else body[:head] + "\n……\n" + body[-tail:]))
    return "\n\n".join(out) or "（找不到稿件）"


# ---------------------------------------------------------------- 命令

def cmd_list(days=bw.RETRO_DAYS[1]):
    base, todo = pending(days)
    print("## 账号基线（各平台全部发布的中位数）")
    print("\n".join(baseline_lines(base)))
    print(f"\n## 待回流的作品（发布 {bw.RETRO_DAYS[0]}–{days} 天、爆款库里还没有记录），共 {len(todo)} 条")
    for w in todo:
        print(f"\n### 《{w['name']}》 发布于 {w['first'] or '—'}")
        print("稿件：" + ("、".join(str(x).replace(str(Path.home()), "~") for x in w["scripts"])
                         or "找不到稿件文件"))
        print("\n".join(metric_lines(w, base)))
    print(f"\n## 爆款库资产（{VAULT.name}），共 {len(vault_rows())} 条")
    print("\n".join(asset_lines()))
    print("\n" + RULES)


PROMPT = """你在做「复盘爆款库」：根据已发布作品的稿件和平台数据，判断每条作品用了爆款库里的哪些资产，并给出结论。

要求：
- 只记稿件里确实用到的资产，拿不准的不记；资产名必须从下面的资产列表里原样照抄。
- 评级为「人设组件」「归因结论」「观察线索」的资产不记。
- {rules}
- 没用到任何资产、或数据不全的作品，放进 skips。
- 稿件里有爆款库没有、但数据明显好的写法，放进 newAssets（只是建议，不会写入）。
- note 写清用了稿件里的哪一句，以及对应指标和倍数，一句话。

【账号基线（各平台全部发布的中位数）】
{baseline}

【爆款库资产】{assets}

【待回流的作品】
{works}

只输出一个 JSON 对象，不要任何其他文字：
{{"records":[{{"asset":"资产全名","work":"作品名","verdict":"优|平|弱","note":"依据"}}],
 "skips":[{{"work":"作品名","note":"原因"}}],
 "newAssets":[{{"category":"分类","name":"资产描述","work":"作品名","reason":"数据依据"}}]}}"""


def build_prompt(days=bw.RETRO_DAYS[1]):
    """返回 (提示词, 待回流作品数)。没有待回流作品时提示词为空。"""
    base, todo = pending(days)
    if not todo:
        return "", 0
    works = "\n\n".join(f"### 《{w['name']}》 发布于 {w['first'] or '—'}\n数据：\n" + "\n".join(metric_lines(w, base))
                        + "\n" + excerpt(w["scripts"]) for w in todo)
    return PROMPT.format(rules=RULES.replace("\n", "\n- "), baseline="\n".join(baseline_lines(base)),
                         assets="\n".join(asset_lines()), works=works), len(todo)


def apply_plan(plan):
    """把复盘建议写进爆款库：先备份，再逐条 record / skip。返回 (写入的消息, 出错的消息)。"""
    import contextlib
    import io
    import shutil
    ensure_vault()
    stamp = date.today().strftime("%Y%m%d")
    backup = VAULT.with_name(f"{VAULT.name}.bak-{stamp}")
    if not backup.exists():
        shutil.copy(VAULT, backup)
    done, errors = [], []
    jobs = [("record", r) for r in plan.get("records") or []] + [("skip", k) for k in plan.get("skips") or []]
    for kind, x in jobs:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if kind == "record":
                    if x.get("verdict") not in ("优", "平", "弱"):
                        raise ValueError(f"结论不是优/平/弱：{x.get('verdict')}")
                    cmd_record(str(x["asset"]), str(x["work"]), x["verdict"], str(x.get("note") or ""))
                else:
                    cmd_skip(str(x["work"]), str(x.get("note") or ""))
            done.append(buf.getvalue().strip())
        except SystemExit:
            errors.append(buf.getvalue().strip() or f"未写入：{x}")
        except Exception as e:                       # 单条出错不影响其他条
            errors.append(f"未写入：{x.get('asset', '')} {x.get('work', '')}（{e}）")
    return done, errors


def cmd_record(asset, work, verdict, note):
    ensure_vault()
    rows = vault_rows()
    hits = [r for r in rows if r[2][0] == asset] or [r for r in rows if r[2][0].startswith(asset)]
    if len(hits) != 1:
        print(f"{'没找到' if not hits else '匹配到多条'}资产「{asset}」，请用完整的资产名。")
        for _, c, cells in hits[:10]:
            print(f"  · [{c}] {cells[0]}")
        sys.exit(1)
    i, _, cells = hits[0]
    _, pubs, *_ = bw.build()
    dates = [p["date"] for p in pubs if p["date"] and bw.work_name([p]) == work]
    if not dates:
        print(f"平台数据里没找到《{work}》，日期记为今天。作品名请用 list 里列出的名字。")
    when = min(dates).strftime("%m-%d") if dates else date.today().strftime("%m-%d")
    entry = f"《{work}》{when}：{note.replace('|', '／')}【{verdict}】"
    old = cells[2]
    if bw.is_recorded(work, bw.recorded_names(old)):
        print(f"「{cells[0]}」已有《{work}》的记录，未重复写入。")
        return
    cells[2] = entry if old in ("", "未回流") else f"{old}；{entry}"
    cells[3] = grade_of(cells[2], cells[3])
    lines = VAULT.read_text(encoding="utf-8").splitlines()
    lines[i] = "| " + " | ".join(cells) + " |"
    VAULT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ {cells[0]} ← {entry}\n  评级：{cells[3]}")


NOTES = "## 回流备注"


def cmd_skip(work, note):
    """复盘过、但没用到库内资产（或数据不全）的作品，记在总库末尾的「回流备注」里。"""
    ensure_vault()
    text = VAULT.read_text(encoding="utf-8")
    if bw.is_recorded(work, bw.recorded_names(text)):
        print(f"《{work}》已有记录，未重复写入。")
        return
    _, pubs, *_ = bw.build()
    dates = [p["date"] for p in pubs if p["date"] and bw.work_name([p]) == work]
    when = min(dates).strftime("%m-%d") if dates else date.today().strftime("%m-%d")
    entry = f"- 《{work}》{when}：{note}"
    lines = text.rstrip("\n").splitlines()
    if NOTES not in lines:
        lines += ["", NOTES, "", "复盘过、但没有用到库内资产或数据不全的作品，不再列为待回流。", ""]
        lines.append(entry)
    else:
        i = lines.index(NOTES)
        j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        lines.insert(j, entry)
    VAULT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ 回流备注 ← {entry}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("list")
    a.add_argument("--days", type=int, default=bw.RETRO_DAYS[1])
    r = sub.add_parser("record")
    r.add_argument("--asset", required=True)
    r.add_argument("--work", required=True)
    r.add_argument("--verdict", required=True, choices=["优", "平", "弱"])
    r.add_argument("--note", required=True)
    k = sub.add_parser("skip")
    k.add_argument("--work", required=True)
    k.add_argument("--note", required=True)
    sub.add_parser("prompt")
    y = sub.add_parser("apply")
    y.add_argument("plan")
    args = ap.parse_args()
    if args.cmd == "list":
        cmd_list(args.days)
    elif args.cmd == "record":
        cmd_record(args.asset, args.work, args.verdict, args.note)
    elif args.cmd == "skip":
        cmd_skip(args.work, args.note)
    elif args.cmd == "prompt":
        print(build_prompt()[0] or "没有待回流的作品")
    elif args.cmd == "apply":
        import json
        done, errors = apply_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")))
        print("\n".join(done + errors))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
