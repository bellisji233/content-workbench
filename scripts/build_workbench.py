#!/usr/bin/env python3
"""内容工作台 — 扫描稿件、流水线与平台数据，对账后生成页面。

用法:  python3 scripts/build_workbench.py            生成并自动在浏览器打开
       python3 scripts/build_workbench.py --no-open 只生成不打开
输出:  local/out/workbench.html（发布到线上用；日常看本地工作台 serve.py）

数据来源（都在 local/config.toml 里配置）:
  1. 流水线（可选）    每期一个文件夹，阶段按配置里的文件是否存在推断
  2. 稿件文件夹        Markdown 成稿
  3. 平台导出          创作者后台导出的 xlsx，每个平台取最新一份
覆盖表:
  local/status.md      手工标注状态与发布关联，优先级高于自动推断
"""

import os
import re
import sys
import glob
import html
import subprocess
import json
from difflib import SequenceMatcher
import unicodedata
from datetime import datetime, date
from pathlib import Path

try:
    import openpyxl
except ImportError:
    raise SystemExit("需要 openpyxl：pip3 install openpyxl")

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (BENCHMARK, CONFIG, DOWNLOAD_INBOX, DRAFTS, EXPORTS,  # noqa: E402
                   INBOX, MINING, OUT_HTML as OUT, OUT_JSON, PROMPTS, STATUS, TEMPLATE,
                   TOPIC_REPORTS, VAULT, WORKBENCH_URL, WORKS)
TODAY = date.today()

# 阶段：没配流水线时只有「文稿 → 发布」两步
STAGE_DEFS = (CONFIG.get("pipeline", {}).get("stages")
              or [{"key": "稿", "label": "文稿"}, {"key": "发", "label": "发布", "published": True}])
STAGES = [(d["key"], d["label"]) for d in STAGE_DEFS]
PUBLISHED_STAGES = [d["key"] for d in STAGE_DEFS if d.get("published")]
DRAFT_DONE = set(CONFIG.get("drafts", {}).get("done", ["稿"]))

# 平台：没配时默认小红书 + 抖音创作者后台的导出格式
PLATFORMS = CONFIG.get("platforms") or [
    {"name": "小红书", "dir": "redbook", "columns": {
        "title": "笔记标题", "date": "首次发布时间", "form": "体裁", "impressions": "曝光",
        "views": "观看量", "ctr": "封面点击率", "likes": "点赞", "comments": "评论",
        "collects": "收藏", "fans": "涨粉", "shares": "分享"}},
    {"name": "抖音", "dir": "douyin", "columns": {
        "title": "作品名称", "date": "发布时间", "form": "体裁", "impressions": "播放量",
        "views": "播放量", "finish5s": "5s完播率", "bounce2s": "2s跳出率", "likes": "点赞量",
        "shares": "分享量", "comments": "评论量", "collects": "收藏量", "fans": "粉丝增量"}},
]


# ---------------------------------------------------------------- utilities

def norm(s):
    """归一化标题：去 emoji、标点、空白，只留中日韩文字与字母数字。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    out = []
    for ch in s:
        if ch.isalnum() or "一" <= ch <= "鿿":
            out.append(ch.lower())
    return "".join(out)


def days_since(ts):
    if not ts:
        return None
    return (TODAY - ts).days


def mtime_date(p):
    try:
        return date.fromtimestamp(os.path.getmtime(p))
    except OSError:
        return None


def newest_mtime(root):
    """目录下最近一次改动（跳过渲染中间产物，它们会掩盖真实活动时间）。"""
    newest = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("renders", "node_modules", ".git")]
        for fn in filenames:
            if fn == ".DS_Store":
                continue
            d = mtime_date(os.path.join(dirpath, fn))
            if d and (newest is None or d > newest):
                newest = d
    return newest


def dir_size_mb(root):
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total / 1024 / 1024


# ---------------------------------------------------------------- status.md

def load_status():
    """读覆盖表：返回 (状态覆盖, 备注, key→平台标题片段)。"""
    overrides, notes, aliases, skips = {}, {}, {}, []
    f = STATUS
    if not f.exists():
        return overrides, notes, aliases, skips

    section = None
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if "覆盖表" in line:
                section = "override"
            elif "不再补稿" in line:
                section = "skip"
            elif "发布关联" in line:
                section = "alias"
            else:
                section = None
            continue
        if not line.strip().startswith("|") or section is None:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("key", "状态") or set(cells[0]) <= set("-: "):
            continue
        if section == "override":
            overrides[cells[0]] = cells[1]
            if len(cells) > 2:
                notes[cells[0]] = cells[2]
        elif section == "skip":
            if cells[0] not in ("标题片段",):
                skips.append((cells[0], cells[1] if len(cells) > 1 else ""))
        else:
            aliases[cells[0]] = cells[1]
    return overrides, notes, aliases, skips


# ---------------------------------------------------------------- platforms

def header_index(rows, title_col):
    """表头所在行：前几行里含标题列名的那一行。导出表头上方常有一行说明。"""
    for i, r in enumerate(rows[:6]):
        if title_col in [str(c).strip() for c in r if c is not None]:
            return i
    return None


_XLSX = {}   # 路径 → (修改时间, 全部行)。本地服务每 5 秒重读一次，没变的表不重新解析


def read_xlsx(f):
    mt = f.stat().st_mtime
    hit = _XLSX.get(f)
    if not hit or hit[0] != mt:
        rows = list(openpyxl.load_workbook(f, read_only=True, data_only=True)
                    .active.iter_rows(values_only=True))
        hit = _XLSX[f] = (mt, rows)
    return hit[1]


def platform_exports(pf):
    """该平台目录下表头对得上的全部导出表，旧的在前。返回 [(路径, 全部行, 表头行号)]。"""
    base = EXPORTS / pf["dir"]
    if not base.exists():
        return []
    title = pf["columns"]["title"]
    files = sorted((p for p in base.rglob("*.xlsx") if not p.name.startswith("~$")),
                   key=lambda p: p.stat().st_mtime)
    out = []
    for f in files:
        rows = read_xlsx(f)
        h = header_index(rows, title)
        if h is not None:
            out.append((f, rows, h))
    return out


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_date(v):
    """2026年09月03日21时 / 2026-09-02 17:00 / Excel 日期，都取年月日。"""
    if isinstance(v, (datetime, date)):
        return v if type(v) is date else v.date()
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", str(v or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


NUM_FIELDS = ("impressions", "views", "likes", "comments", "collects", "fans", "shares")
PCT_FIELDS = ("ctr", "finish5s", "bounce2s")


def parse_export(pf, rows, h):
    header = [str(c).strip() if c is not None else "" for c in rows[h]]
    col = {field: header.index(name) for field, name in pf["columns"].items() if name in header}
    get = lambda r, f: r[col[f]] if f in col and col[f] < len(r) else None  # noqa: E731
    items = []
    for r in rows[h + 1:]:
        if not r or not get(r, "title"):
            continue
        rec = {"platform": pf["name"], "title": str(get(r, "title")).replace("\n", " "),
               "date": parse_date(get(r, "date")), "form": str(get(r, "form") or "")}
        for f in NUM_FIELDS:
            rec[f] = num(get(r, f))
        for f in PCT_FIELDS:
            if f in col:
                rec[f] = pct(get(r, f))
        items.append(rec)
    return items


def load_platform(pf):
    """读一个平台的全部导出表并合并。返回 (发布记录列表, 最新一份导出表路径)。

    后台导出常只含最近一段时间的作品，只读最新一份会丢掉更早的发布。
    所以每份都读：同一条作品（发布日期 + 标题开头）以最新一份的数据为准，只在旧表里出现的保留。
    """
    files = platform_exports(pf)
    if not files:
        return [], None
    merged = {}
    for _, rows, h in files:                       # 旧 → 新，新的覆盖旧的
        for rec in parse_export(pf, rows, h):
            merged[(rec["date"], norm(rec["title"])[:15])] = rec
    items = sorted(merged.values(), key=lambda r: r["date"] or date.min, reverse=True)
    return items, files[-1][0]


def load_platforms():
    return [{"name": pf["name"], "rows": rows, "path": path}
            for pf in PLATFORMS for rows, path in [load_platform(pf)]]



# ---------------------------------------------------------------- 爆款资产库

def auto_merge_assets():
    """把工作台导出的分析合并进资产总库。

    和采集箱的自动收件同理：导出后不该再记一条命令，
    下次生成看板时顺手并掉。细节在 merge_assets.py。
    """
    try:
        import merge_assets
        return merge_assets.auto_merge()
    except Exception:
        return 0


def load_assets():
    """读跨批次资产总库：按分类拆出每条资产的来源、使用记录与评级。"""
    merged = auto_merge_assets()
    f = VAULT
    if not f.exists():
        return {"categories": [], "baseline": "", "batches": [], "justMerged": merged}

    cats, cur, baseline = [], None, []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("> ") and ("基线" in line or "平台特性" in line):
            baseline.append(line[2:].strip())
        if line.startswith("## "):
            cur = {"name": line[3:].strip(), "assets": []}
            if cur["name"] != "回流备注":          # 复盘爆款库记下的备注，不是资产分类
                cats.append(cur)
            continue
        if not line.strip().startswith("|") or cur is None:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] == "资产" or set(cells[0]) <= set("-: "):
            continue
        grade = cells[3]
        tier = ("top" if grade.startswith("✅") else
                "down" if grade.startswith("⚠️") else
                "persona" if "人设组件" in grade or "归因结论" in grade else "untested")
        # 来源写「同上」时沿用上一条的来源
        source = cells[1]
        if source in ("同上", "〃") and cur["assets"]:
            source = cur["assets"][-1]["source"]
        cur["assets"].append({
            "name": cells[0].lstrip("⚠️▶ ").strip(), "source": source,
            "record": cells[2], "grade": grade, "tier": tier,
            "used": "未回流" not in cells[2],
        })

    # 每个挖掘批次拆了几条样本（有 transcript 就算一条）
    batches = []
    wd = MINING / "work" if MINING else None
    if wd and wd.exists():
        for d in sorted(wd.iterdir()):
            if not d.is_dir():
                continue
            tx = list((d / "素材").glob("*_transcript.txt")) if (d / "素材").exists() else []
            batches.append({"name": d.name, "samples": len(tx),
                            "report": (d / "框架报告.html").exists()})
    return {"categories": cats, "baseline": baseline, "batches": batches,
            "justMerged": merged}



def _split_sections(text, level):
    """按指定层级的标题切段，返回 [(标题, 正文), ...]。"""
    prefix = "#" * level + " "
    out, title, buf = [], None, []
    for line in text.splitlines():
        if line.startswith(prefix) and not line.startswith(prefix + "#"):
            if title is not None:
                out.append((title, "\n".join(buf).strip()))
            title, buf = line[len(prefix):].strip(), []
        elif title is not None:
            buf.append(line)
    if title is not None:
        out.append((title, "\n".join(buf).strip()))
    return out


def load_reports():
    """把两批拆解报告解析成「批次 → 样本 → 模块」三层结构。

    两批格式不一样：
      2026-06 批   框架归纳.md    3 个样本，### 样本 / #### 维度，另有共性框架
      2026-07-16 批 解构-s1.md    1 个样本，## 模块N：名，另有可迁移资产候选
    """
    wd = MINING / "work" if MINING else None
    if not wd or not wd.exists():
        return []

    import trash
    hidden = trash.hidden_reports()       # 页面上删除的条目，键为「批次|样本名」
    batches = []
    for d in sorted(wd.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        batch = {"name": d.name, "path": str(d).replace(str(Path.home()), "~"),
                 "samples": [], "common": [], "reportHtml": None, "overview": []}
        if (d / "框架报告.html").exists():
            batch["reportHtml"] = str(d / "框架报告.html").replace(str(Path.home()), "~")

        # --- 格式 A：框架归纳.md（多样本）---
        f = d / "框架归纳.md"
        if f.exists():
            text = f.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("> 生成日期"):
                    batch["generated"] = line.split("：")[-1].strip()
                if line.startswith("| s") and line.count("|") >= 7:
                    c = [x.strip() for x in line.strip().strip("|").split("|")]
                    batch["overview"].append({
                        "sample": c[0], "duration": c[1], "kind": c[2], "audience": c[3],
                        "angle": c[4], "borrow": c[5], "weakness": c[6]})
            for h2, body2 in _split_sections(text, 2):
                if h2.startswith("2."):
                    for h3, body3 in _split_sections(body2, 3):
                        mods = [{"name": n, "body": b} for n, b in _split_sections(body3, 4) if b]
                        if mods:
                            batch["samples"].append({"name": h3, "modules": mods})
                elif h2.startswith("3."):
                    batch["common"] = [{"name": n, "body": b}
                                       for n, b in _split_sections(body2, 3) if b]

        # --- 格式 B：解构-*.md（单样本，模块编号在 ## 上）---
        for f in sorted(d.glob("解构-*.md")):
            text = f.read_text(encoding="utf-8")
            head = text.split("\n##", 1)[0]
            name = head.splitlines()[0].lstrip("# ").strip() if head else f.stem
            meta = [l.lstrip("- ").strip() for l in head.splitlines()[1:] if l.strip().startswith("-")]
            mods = [{"name": n, "body": b} for n, b in _split_sections(text, 2) if b]
            if mods:
                batch["samples"].append({"name": name, "meta": meta, "modules": mods})

        batch["samples"] = [x for x in batch["samples"] if f"{d.name}|{x['name']}" not in hidden]
        if f"{d.name}|共性框架" in hidden:
            batch["common"] = []
        if batch["samples"] or batch["common"]:
            batches.append(batch)
    return batches


REPORT_KINDS = {
    "自有内容": {"label": "自有内容选题分析", "cmd": "python3 scripts/topic_report.py",
               "desc": "基于已发布数据，评估各选题方向的真实转化"},
}


def load_topic_reports():
    """历次选题分析报告。文件名形如 2026-09-08-自有内容.md。"""
    d = TOPIC_REPORTS
    out = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md"), reverse=True):
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(.+)", f.stem)
        if not m:
            continue
        text = f.read_text(encoding="utf-8")
        # 摘一句结论当列表里的提要
        lead = ""
        for line in text.splitlines():
            if line.startswith("**主力方向"):
                lead = line.strip()
                break
        out.append({"date": m.group(1), "kind": m.group(2),
                    "label": REPORT_KINDS.get(m.group(2), {}).get("label", m.group(2)),
                    "path": f"local/data/topic-reports/{f.name}", "lead": lead, "body": text})
    return out


def auto_ingest():
    """把插件刚采的笔记从下载目录搬进来。

    每次生成看板都先跑一遍，所以「采集 → 重跑看板 → 出现在采集箱」不需要
    额外记一条命令。搬运细节与去重逻辑在 ingest_inbox.py 里。
    """
    src = DOWNLOAD_INBOX
    if not src.exists() or not any(src.glob("*.json")):
        return 0
    try:
        import ingest_inbox
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            ingest_inbox.main()
        return 1
    except Exception:
        return 0


def auto_transcribe():
    """发现未转写的视频笔记就在后台起转写。

    转写是纯本地流程（下载 → ffmpeg → faster-whisper），不需要大模型，
    但一条 6 分钟的视频要跑几分钟，所以放后台，不挡着看板生成。
    转完重跑一次看板就能看到口播全文。
    """
    d = INBOX
    if not d.exists():
        return []
    lock = d / "_media" / ".running"
    if lock.exists():
        return [l for l in lock.read_text(encoding="utf-8").splitlines() if l]

    pending = []
    for f in d.glob("*.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if p.get("type") == "视频" and not p.get("transcriptText") \
                and (p.get("video") or {}).get("url"):
            pending.append(p.get("title") or f.stem)
    if not pending:
        return []
    subprocess.Popen([sys.executable, str(Path(__file__).parent / "transcribe_inbox.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return pending


def load_benchmark():
    """对标账号的汇总与更新历史（插件采集后由 ingest_inbox.py 写入）。"""
    d = BENCHMARK
    out = {"summary": None, "history": []}
    if not d.exists():
        return out
    f = d / "_summary.json"
    if f.exists():
        try:
            out["summary"] = json.loads(f.read_text(encoding="utf-8"))
            fetched = out["summary"].get("fetchedAt")
            if fetched:
                out["summary"]["staleDays"] = (TODAY - date.fromisoformat(fetched)).days
        except (json.JSONDecodeError, ValueError):
            pass
    h = d / "_history.json"
    if h.exists():
        try:
            out["history"] = json.loads(h.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return out


def load_inbox():
    """插件采集、已收进 local/data/inbox/ 的笔记。"""
    auto_ingest()
    d = INBOX
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = p.get("stats", {})
        out.append({
            "id": p.get("noteId") or f.stem,
            "title": p.get("title") or f.stem, "author": p.get("author", {}).get("name", ""),
            "type": p.get("type"), "url": p.get("url"),
            "likes": st.get("likes", 0), "collects": st.get("collects", 0),
            "commentCount": st.get("comments", 0), "tags": p.get("tags", [])[:8],
            "capturedAt": (p.get("capturedAt") or "")[:10],
            "publishedAt": (p.get("publishedAt") or "")[:10],
            "body": p.get("body") or "",
            "topComments": [{"author": c.get("author", ""), "text": c.get("text", ""),
                             "likes": c.get("likes", 0)}
                            for c in (p.get("comments") or [])[:15]],
            "transcript": p.get("transcriptText") or "",
            "transcriptLines": len(p.get("transcript") or []),
            # 视频笔记只有转写完才算拿到真正的口播，否则分析的是简介
            "needsTranscript": p.get("type") == "视频" and not p.get("transcriptText"),
            "collectRate": round(st.get("collects", 0) / st.get("likes", 1) * 100, 1)
                           if st.get("likes") else 0,
        })
    return out


# ---------------------------------------------------------------- sources

def scan_works():
    """流水线：每期一个文件夹，阶段按配置里的文件是否存在推断。"""
    items = []
    if not WORKS or not WORKS.exists():
        return items
    junk = CONFIG.get("pipeline", {}).get("junk", [])
    for d in sorted(WORKS.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        has = {s["key"]: any(glob.glob(str(d / pat)) for pat in s.get("files", []))
               for s in STAGE_DEFS if not s.get("published")}
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(.+)", d.name)
        items.append({
            "key": d.name, "track": "流水线", "path": f"{WORKS.name}/{d.name}/",
            "title": m.group(4) if m else d.name,
            "date": date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None,
            "stages": has, "touched": newest_mtime(d),
            "junk_mb": sum(dir_size_mb(d / j) for j in junk if (d / j).exists()),
        })
    return items


def scan_drafts():
    """稿件文件夹（如 Obsidian）里的 Markdown 稿件。"""
    items = []
    if not DRAFTS or not DRAFTS.exists():
        return items
    for p in sorted(DRAFTS.glob("*.md")):
        stem = p.stem
        body = p.read_text(encoding="utf-8", errors="ignore")
        # 空白稿和「未命名」占位文件不算内容单元
        if stem.startswith("未命名") or len(body.strip()) < 50:
            continue
        # 文件名带日期前缀（2026-08-19 标题 / 草稿 标题），key 用去掉前缀的标题，
        # 这样重命名不会打断 status.md 的别名与网页上已存的状态。
        name = re.sub(r"^(\d{4}-\d{2}-\d{2}|草稿)\s+", "", stem)
        items.append({
            "key": name, "track": "手写",
            "path": str(p).replace(str(Path.home()), "~"),
            "title": name, "date": mtime_date(p),
            "stages": {k: k in DRAFT_DONE for k, _ in STAGES if k not in PUBLISHED_STAGES},
            "touched": mtime_date(p), "junk_mb": 0, "words": len(body),
            "abandoned": name.startswith("废弃"),
        })
    return items


# ---------------------------------------------------------------- join

def match_publications(item, pubs, aliases):
    """把一个内容单元关联到平台上的发布记录。

    别名表命中优先；没有别名时才做模糊回退。已标废弃的稿子不参与模糊匹配——
    它们标题常和正式发布的内容相似，会误配。
    """
    frag = aliases.get(item["key"])
    if frag:
        nf = norm(frag)
        hits = [p for p in pubs if nf and nf in norm(p["title"])]
        if hits:
            return closest_per_platform(norm(item["title"]), hits)
    if item.get("abandoned"):
        return []
    nt = norm(item["title"])
    if len(nt) < 6:
        return []

    def hit(p, k):
        np_ = norm(p["title"])
        return nt[:k] in np_ or np_[:k] in nt

    return closest_per_platform(nt, [p for p in pubs if hit(p, 10)])


def closest_per_platform(nt, hits):
    """同一平台命中多条时（系列标题前缀相同、别名片段太宽），只留和标题最像的；同样像的都留。"""
    by_pf = {}
    for p in hits:
        by_pf.setdefault(p["platform"], []).append(p)
    out = []
    for ps in by_pf.values():
        if len(ps) == 1:
            out += ps
            continue
        score = [SequenceMatcher(None, nt, norm(p["title"])).ratio() for p in ps]
        out += [p for p, sc in zip(ps, score) if sc >= max(score) - 0.02]
    return out


# 表示「还没发出去」的状态。有发布记录时，这些手动标记让位给「已发布」
PRE_PUBLISH = {"?", "生产中", "待发布", "未发布", "主动停掉", "效果不好"}


def build():
    overrides, notes, aliases, skips = load_status()
    platforms = load_platforms()
    pubs = [r for pf in platforms for r in pf["rows"]]

    items = scan_works() + scan_drafts()
    matched_titles = set()

    for it in items:
        hits = match_publications(it, pubs, aliases)
        it["pubs"] = hits
        for h in hits:
            matched_titles.add((h["platform"], h["title"]))
        for k in PUBLISHED_STAGES:
            it["stages"][k] = bool(hits)

        ov = overrides.get(it["key"])
        it["note"] = notes.get(it["key"], "")
        # 状态表里「还没发」一类的标记，一旦导出数据里出现了发布记录就以数据为准
        if ov and not (hits and ov in PRE_PUBLISH):
            it["status"] = ov
        elif it.get("abandoned"):
            it["status"] = "归档"
        elif hits:
            it["status"] = "已发布"
        elif it["track"] == "手写":
            it["status"] = "待发布"
        else:
            it["status"] = "未发布"

        st = it["stages"]
        done = [k for k, _ in STAGES if st.get(k)]
        pending = [lbl for k, lbl in STAGES if not st.get(k)]
        it["stuck_at"] = pending[0] if pending else None
        it["progress"] = len(done)
        it["idle"] = days_since(it["touched"])

    orphans = [p for p in pubs if (p["platform"], p["title"]) not in matched_titles]
    for p in orphans:
        # 「全部 YYYY-MM 之前」这类整段跳过规则，按日期判定
        p["skipped"] = None
        for frag, why in skips:
            m = re.match(r"全部\s*(\d{4})-(\d{2})\s*之前", frag)
            if m:
                cutoff = date(int(m.group(1)), int(m.group(2)), 1)
                if p["date"] and p["date"] < cutoff:
                    p["skipped"] = why or "已决定不补"
                    break
            elif norm(frag) and norm(frag) in norm(p["title"]):
                p["skipped"] = why or "已决定不补"
                break

    # 同一条发布被多个稿子认领 = 同一内容在两条产线各存了一份
    claims = {}
    for it in items:
        for h in it["pubs"]:
            claims.setdefault((h["platform"], h["title"]), []).append(it)
    dupes = []
    seen = set()
    for (plat, title), owners in claims.items():
        if len(owners) < 2:
            continue
        sig = tuple(sorted(o["key"] for o in owners))
        if sig in seen:
            continue
        seen.add(sig)
        tracks = {o["track"] for o in owners}
        dupes.append({
            "title": title, "owners": owners,
            # 同一条产线里出现两份 = 真重复；两条产线各一份 = 正常分工
            "kind": "same" if len(tracks) == 1 else "cross",
        })

    return items, pubs, orphans, dupes, platforms


# ---------------------------------------------------------------- export

RETRO_DAYS = (3, 90)   # 发布满 3 天数据才稳定；超过 90 天的不再追溯


def work_name(pubs):
    """一条作品的名字，爆款库的使用记录用它写《》。

    取最短的那个平台标题（抖音的「作品名称」常连着整段描述），在第一个问号、分隔符处截断，最多 30 个字。
    """
    t = re.sub(r"\s+", " ", min((p["title"] for p in pubs), key=len)).strip()
    m = re.match(r"(.{8,}?)\s*[？?！!。|｜｢「#]", t)
    return (m.group(1) if m else t)[:30].strip(" ,，:：")


def recorded_names(text):
    """爆款库里出现过的作品名（《》里的内容），去掉括号注释后归一化。"""
    return {norm(re.sub(r"[（(][^）)]*[）)]", "", n)) for n in re.findall(r"《([^》]+)》", text)}


def is_recorded(name, names):
    """作品是否已有记录：归一化后一方是另一方的开头（至少 6 个字）。兼容早期手写的简写名。"""
    nw = norm(name)
    return any(len(min(nr, nw, key=len)) >= 6 and (nw.startswith(nr) or nr.startswith(nw)) for nr in names)


def published_works(items):
    """按发布记录归并：同一条发布可能被流水线和稿件文件夹各认领一份。返回 [{name, pubs, items}]。"""
    works = {}
    for it in items:
        if not it["pubs"]:
            continue
        key = tuple(sorted((p["platform"], p["title"]) for p in it["pubs"]))
        w = works.setdefault(key, {"name": work_name(it["pubs"]), "pubs": it["pubs"], "items": []})
        w["items"].append(it)
    return list(works.values())


def retro_pending(items):
    """发布 3–90 天、爆款库里还没有使用记录的作品数。"""
    names = recorded_names(VAULT.read_text(encoding="utf-8")) if VAULT.exists() else set()
    n = 0
    for w in published_works(items):
        dates = [p["date"] for p in w["pubs"] if p["date"]]
        if dates and not is_recorded(w["name"], names) \
                and RETRO_DAYS[0] <= (TODAY - min(dates)).days <= RETRO_DAYS[1]:
            n += 1
    return n


def source_dir(key):
    """数据来源 key → 文件夹。本地服务「打开文件夹」只认这几个 key，不接受页面传来的路径。"""
    if key.startswith("export:"):
        i = int(key.split(":", 1)[1])
        return EXPORTS / PLATFORMS[i]["dir"] if 0 <= i < len(PLATFORMS) else None
    return {"drafts": DRAFTS, "works": WORKS}.get(key)


def data_sources(items, platforms):
    """总览「数据来源」：每类数据从哪个文件夹读、读到了什么。路径不进载荷，页面只拿 key。"""
    out = []
    for i, pf in enumerate(platforms):
        p = pf["path"]
        out.append({"key": f"export:{i}", "label": f"{pf['name']}数据", "kind": "export",
                    "ready": bool(p), "count": len(pf["rows"]),
                    "date": mtime_date(p).isoformat() if p else None})
    if DRAFTS:
        out.append({"key": "drafts", "label": "稿件", "kind": "drafts", "ready": DRAFTS.exists(),
                    "count": sum(1 for i in items if i["track"] == "手写")})
    if WORKS:
        out.append({"key": "works", "label": "生产流水线", "kind": "works", "ready": WORKS.exists(),
                    "count": sum(1 for i in items if i["track"] == "流水线")})
    return out


def to_payload(items, pubs, orphans, dupes, platforms):
    """把扫描结果压成一个 JSON 载荷，交给前端模板渲染。

    废弃稿件不进载荷（用户要求前端不展示），只在 meta 里留个计数。
    """
    import classify

    def pub_json(p):
        return {
            "platform": p["platform"], "title": p["title"], "cat": classify.classify(p["title"]),
            "date": p["date"].isoformat() if p["date"] else None,
            "views": p["views"], "likes": p["likes"], "collects": p["collects"],
            "comments": p["comments"], "fans": p["fans"],
            "finish5s": p.get("finish5s"), "bounce2s": p.get("bounce2s"),
            "skipped": p.get("skipped"),
        }

    live = [i for i in items if not i.get("abandoned")]
    archived = len(items) - len(live)

    out_items = []
    for i in live:
        out_items.append({
            "key": i["key"], "track": i["track"], "title": i["title"],
            "path": i["path"], "date": i["date"].isoformat() if i["date"] else None,
            "stages": {k: bool(i["stages"].get(k)) for k, _ in STAGES},
            "stuckAt": i["stuck_at"], "progress": i["progress"],
            "status": i["status"], "note": i["note"], "idle": i["idle"],
            "words": i.get("words", 0), "junkMb": round(i["junk_mb"], 1),
            "pubs": [pub_json(p) for p in i["pubs"]],
        })

    def platform_stats(arr, name):
        views = sorted(x["views"] for x in arr)
        return {
            "name": name, "n": len(arr),
            "views": sum(views), "median": views[len(views) // 2] if views else 0,
            "collects": sum(x["collects"] for x in arr),
            "fans": sum(x["fans"] for x in arr),
        }

    return {
        "generated": TODAY.isoformat(),
        "items": out_items,
        "orphans": [pub_json(p) for p in orphans],
        "dupes": [{"title": d["title"], "kind": d["kind"],
                   "owners": [{"path": o["path"], "track": o["track"]} for o in d["owners"]]}
                  for d in dupes],
        "platforms": [platform_stats(pf["rows"], pf["name"]) for pf in platforms],
        "topics": classify.state([r for pf in platforms for r in pf["rows"]]),
        "stages": [{"key": k, "label": label} for k, label in STAGES],
        "prompts": {f.stem: f.read_text(encoding="utf-8").rstrip("\n")
                    for f in sorted(PROMPTS.glob("*.md")) if f.stem != "README"},
        "account": CONFIG.get("account", {}),
        "statusExtra": CONFIG.get("status", {}).get("extra", []),
        "assets": load_assets(),
        "inbox": load_inbox(),
        "benchmark": load_benchmark(),
        "transcribing": auto_transcribe(),
        "reports": load_reports(),
        "topicReports": load_topic_reports(),
        "reportKinds": REPORT_KINDS,
        "workbenchUrl": WORKBENCH_URL,
        "sources": data_sources(items, platforms),
        "retroPending": retro_pending(items),
        "meta": {
            "archived": archived,
            "exports": [{"platform": pf["name"],
                         "file": str(pf["path"].relative_to(EXPORTS)) if pf["path"] else None}
                        for pf in platforms],
            "junkMb": round(sum(i["junk_mb"] for i in items)),
        },
    }


def render(payload):
    """把数据填进页面模板。本地服务（serve.py）和静态生成共用。"""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # 笔记正文里出现 </script> 会提前结束脚本块，转义成 JSON 里等价的 <\/
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("__WORKBENCH_DATA__", blob)


def write_app(payload):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(payload), encoding="utf-8")


if __name__ == "__main__":
    data = build()
    payload = to_payload(*data)
    write_app(payload)
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    items, pubs, orphans, dupes, _ = data
    works = [i for i in payload["items"] if i["track"] == "流水线"]
    print(f"✓ {OUT}")
    print(f"  流水线 {len(works)} 期 · 手写 {len(payload['items']) - len(works)} 篇 · "
          f"已发布 {len(pubs)} 条（隐藏废弃 {payload['meta']['archived']} 篇）")
    print(f"  待确认 {len([i for i in payload['items'] if i['status'] == '?'])} 期 · "
          f"重复 {len(dupes)} 组 · 无本地稿 {len(orphans)} 条")
    if "--no-open" not in sys.argv:
        subprocess.run(["open", str(OUT)], check=False)
