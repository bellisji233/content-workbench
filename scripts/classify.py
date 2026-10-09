"""选题方向 —— 报告与看板共用同一套口径。

方向不是预设的：第一次由 AI 读这个账号的全部作品标题，划分 4–8 个方向并逐条归类；
之后放进新导出，只给新作品归类，方向保持不变，报告前后才能对比。
内容变了可以「重新划分」，还可以附一句要求（比如「把测评和教程分开」）。
单条分错了可以手动改，手动改的以后不会被 AI 覆盖。

结果存在 local/data/topics.json：
  categories  [{name, desc}]
  assign      {作品键: {title, cat, by: ai | user}}

用法:
  python3 scripts/classify.py                给还没归类的作品归类（第一次会先划分方向）
  python3 scripts/classify.py partition 要求  重新划分方向，要求可省略
  python3 scripts/classify.py list           列出方向和每条作品的归类
  python3 scripts/classify.py undo           回到上一次划分

旧配置里的 [[categories]] 关键词规则仍然认：没有 AI 划分结果时按规则归类，
第一次划分时把这些方向名交给 AI 作为参考。
"""

import hashlib
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CONFIG, PROMPTS, TOPICS  # noqa: E402

OTHER = "其他"          # AI 判断哪个方向都不沾边
PENDING = "未分类"      # 还没归过类
NO_VERDICT = {OTHER, PENDING}
PREV = TOPICS.with_name("topics.prev.json")
BATCH = 150             # 一次调用最多归类多少条
_lock = threading.Lock()


# ---------------------------------------------------------------- 存取

def key_of(title):
    """作品键：去掉空白后的标题指纹。标题改了就当新作品重新归类。"""
    return hashlib.sha1(re.sub(r"\s+", "", title or "").encode("utf-8")).hexdigest()[:12]


def load():
    try:
        d = json.loads(TOPICS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("categories", [])
    d.setdefault("assign", {})
    return d


def save(d):
    TOPICS.parent.mkdir(parents=True, exist_ok=True)
    d["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = TOPICS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(TOPICS)
    _cache["data"] = None


def _legacy_rules():
    out = []
    for c in CONFIG.get("categories", []):
        if c.get("pattern"):
            out.append((c["name"], c["pattern"]))
        elif c.get("keywords"):
            out.append((c["name"], "|".join(re.escape(k) for k in c["keywords"])))
    return out


RULES = _legacy_rules()
_cache = {"mtime": None, "data": None}


def _current():
    """读 topics.json，文件没变就用缓存：报告和页面每条作品都要查一次。"""
    try:
        m = TOPICS.stat().st_mtime
    except OSError:
        m = None
    if _cache["mtime"] != m or _cache["data"] is None:
        _cache["mtime"], _cache["data"] = m, load()
    return _cache["data"]


def classify(title):
    d = _current()
    a = d["assign"].get(key_of(title))
    if a:
        return a["cat"]
    if not d["categories"]:
        for name, pat in RULES:
            if re.search(pat, title or "", re.I):
                return name
        return OTHER if RULES else PENDING
    return PENDING


def categories():
    return _current()["categories"]


# ---------------------------------------------------------------- 统计

def median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else 0


def summarize(rows):
    """rows 是若干条发布记录，算出这一组的表现指标。"""
    views = [r["views"] for r in rows]
    total_views = sum(views)
    collects = sum(r["collects"] for r in rows)
    fans = sum(r["fans"] for r in rows)
    return {
        "n": len(rows),
        "views": total_views,
        "median": median(views),
        "collects": collects,
        "fans": fans,
        # 收藏率＝沉淀意愿，涨粉/千播＝转化效率，两个都比绝对播放量更能说明选题好坏
        "collectRate": collects / total_views * 100 if total_views else 0,
        "fanRate": fans / total_views * 1000 if total_views else 0,
    }


def group_by_type(pubs):
    groups = {}
    for p in pubs:
        groups.setdefault(classify(p["title"]), []).append(p)
    return groups


# ---------------------------------------------------------------- 作品与状态

def all_items():
    """全部发布作品，按标题去重：[{key, title, platform}]。"""
    import build_workbench as bw
    seen, out = set(), []
    for pf in bw.load_platforms():
        for r in pf["rows"]:
            k = key_of(r["title"])
            if r["title"] and k not in seen:
                seen.add(k)
                out.append({"key": k, "title": r["title"], "platform": pf["name"]})
    return out


def pending(items):
    d = _current()
    return [i for i in items if i["key"] not in d["assign"]]


def needs_work(items):
    """有作品还没归类（第一次用、或放进了新导出）。只有旧关键词规则时不算。"""
    if not items:
        return False
    if not _current()["categories"]:
        return not RULES
    return bool(pending(items))


# ---------------------------------------------------------------- 调 AI

def _prompt(name, **vars):
    tpl = (PROMPTS / "classify" / f"{name}.md").read_text(encoding="utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(vars.get(m.group(1), m.group(0))), tpl)


def _lines(items):
    return "\n".join(f"{n} · {i['platform']} · {re.sub(r'\s+', ' ', i['title'])[:150]}"
                     for n, i in enumerate(items, 1))


def _json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("AI 返回的不是 JSON")
    return json.loads(text[a:b + 1])


def _ask(prompt):
    import agent
    try:
        return _json(agent.run(prompt))
    except (ValueError, json.JSONDecodeError):
        # 偶尔会多写几句或格式不全，再要一次
        return _json(agent.run(prompt + "\n\n上一次的输出不是合法 JSON。这次只输出 JSON。"))


def _assign_batch(items, cats):
    names = {c["name"] for c in cats}
    lines = "\n".join(f"- {c['name']}：{c.get('desc', '')}" for c in cats)
    got = {}
    for s in range(0, len(items), BATCH):
        chunk = items[s:s + BATCH]
        res = _ask(_prompt("assign", categories=lines, titles=_lines(chunk))).get("assign", {})
        for n, i in enumerate(chunk, 1):
            cat = str(res.get(str(n), "")).strip()
            got[i["key"]] = cat if cat in names else OTHER
    return got


def assign_new(items, log=print):
    """把还没归类的作品归入已有方向。"""
    todo = pending(items)
    if not todo:
        return 0
    cats = _current()["categories"]
    log(f"给 {len(todo)} 条新作品归类…")
    got = _assign_batch(todo, cats)
    with _lock:
        d = load()
        for i in todo:
            if i["key"] not in d["assign"]:
                d["assign"][i["key"]] = {"title": i["title"][:80], "cat": got[i["key"]], "by": "ai"}
        save(d)
    return len(todo)


def partition(items, hint="", log=print):
    """划分方向并给全部作品归类。手动改过、且方向还在的保留。"""
    if not items:
        return None
    acc = CONFIG.get("account", {})
    old = load()
    seed_names = [c["name"] for c in old["categories"]] or [n for n, _ in RULES]
    seed = (f"\n【现有方向】{'、'.join(seed_names)}\n合适的沿用原名；不合适的可以拆分、合并或改名。\n"
            if seed_names else "")
    sample = items[:250]                  # 作品很多时用前 250 条定方向，其余按方向归类
    log(f"按 {len(sample)} 条作品划分选题方向…")
    res = _ask(_prompt("partition", accountName=acc.get("name") or "（未填写）",
                       accountPositioning=acc.get("positioning") or "（未填写）",
                       count=len(sample), titles=_lines(sample), seed=seed,
                       hint=f"\n【作者对划分的要求】{hint.strip()}\n" if hint.strip() else ""))
    cats, seen = [], set()
    for c in res.get("categories", []):
        name = str(c.get("name", "")).strip()[:12]
        if name and name not in seen and name not in NO_VERDICT:
            seen.add(name)
            cats.append({"name": name, "desc": str(c.get("desc", "")).strip()})
    if len(cats) < 2:
        raise ValueError("AI 没有给出可用的选题方向")
    got = {}
    for n, i in enumerate(sample, 1):
        cat = str(res.get("assign", {}).get(str(n), "")).strip()
        if cat in seen:
            got[i["key"]] = cat
    rest = [i for i in items if i["key"] not in got]
    if rest:
        log(f"再给 {len(rest)} 条作品归类…")
        got.update(_assign_batch(rest, cats))

    with _lock:
        cur = load()
        if cur["categories"]:
            PREV.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        assign = {i["key"]: {"title": i["title"][:80], "cat": got.get(i["key"], OTHER), "by": "ai"}
                  for i in items}
        for k, a in cur["assign"].items():          # 手动改过的：方向还在就保留
            if a.get("by") == "user" and a.get("cat") in seen | {OTHER}:
                assign[k] = a
        save({"categories": cats, "assign": assign, "hint": hint.strip()})
    return cats


def ensure(items, log=print):
    """报告前调用：第一次先划分，之后只给新作品归类。返回是否调用了 AI。"""
    if not needs_work(items):
        return False
    if not _current()["categories"]:
        partition(items, log=log)
    else:
        assign_new(items, log=log)
    return True


def set_category(title, cat):
    """手动改一条作品的方向。"""
    with _lock:
        d = load()
        if cat not in {c["name"] for c in d["categories"]} | {OTHER}:
            raise ValueError(f"没有「{cat}」这个方向")
        d["assign"][key_of(title)] = {"title": title[:80], "cat": cat, "by": "user"}
        save(d)


def undo():
    """回到上一次划分。"""
    with _lock:
        if not PREV.exists():
            raise ValueError("没有上一次的划分")
        PREV.replace(TOPICS)
        _cache["data"] = None


def state(pubs):
    """给页面的摘要：方向、每个方向的作品数、未归类数。"""
    d = _current()
    count = {}                        # 和报告同一口径：每个平台的每条发布算一条
    for p in pubs:
        c = classify(p["title"])
        count[c] = count.get(c, 0) + 1
    return {"categories": [{**c, "n": count.get(c["name"], 0)} for c in d["categories"]],
            "other": count.get(OTHER, 0), "pending": count.get(PENDING, 0),
            "legacy": not d["categories"] and bool(RULES),
            "hint": d.get("hint", ""), "updatedAt": d.get("updatedAt", ""), "hasPrev": PREV.exists()}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    items = all_items()
    if cmd == "partition":
        cats = partition(items, " ".join(sys.argv[2:]))
        print("✓ " + "、".join(c["name"] for c in cats))
    elif cmd == "undo":
        undo()
        print("✓ 已回到上一次划分")
    elif cmd == "list":
        for c in categories():
            print(f"■ {c['name']}：{c.get('desc', '')}")
        for i in sorted(items, key=lambda i: classify(i["title"])):
            print(f"  {classify(i['title'])}\t{i['platform']}\t{i['title'][:50]}")
    else:
        if not ensure(items):
            print(f"✓ {len(items)} 条作品都已归类")
        else:
            print("✓ 已归类：" + "、".join(f"{c['name']} {n}" for c in categories()
                                          for n in [sum(1 for i in items if classify(i["title"]) == c["name"])]))
