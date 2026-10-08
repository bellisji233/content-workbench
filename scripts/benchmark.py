"""对标账号数据：落盘、汇总、分析。

数据来自 Chrome 插件：手动「抓取这个主页」，或插件里的「依次更新对标账号」（实验功能）。
两种都落到下载目录，经 ingest_inbox.py 收进 local/data/benchmark/{userId}.json。

每次有账号更新，write_summary() 重算全部账号的汇总给工作台读。
"""

import json
import re
import statistics
from datetime import date

from paths import BENCHMARK, CONFIG

# 「近期爆款」统计最近多少天；更早的归入「更早的高表现」
RECENT_DAYS = int(CONFIG.get("benchmark", {}).get("recent_days", 30))


# 标题模式：用来看这批账号在标题层用什么套路、哪种真的有效
TITLE_PATTERNS = [
    ("教程/上手", r"教程|上手|从\s*0|入门|保姆|手把手|实测|讲清楚|搞懂|必读|怎么用"),
    ("数字承诺", r"^\d+|[一二三四五六七八九十\d]+\s*(个|句|步|种|条|款|分钟|天)"),
    ("疑问句", r"[?？]|什么是|怎么|如何|为什么|哪些"),
    ("情绪/感叹", r"[!！]|绝|太|超|真的|居然|终于|惊艳|好可爱|夯"),
    ("工具/skill", r"skill|agent|prompt|工作流|自动化|搭建|做了个|手搓|工具"),
    ("系列化编号", r"[（(]\d+[/／]\d+[)）]|[（(]\s*\d+\s*[)）]|第\s*\d+\s*[期讲]"),
    ("身份视角", r"文科|小白|零基础|下班|普通人|新手|前\s*\S{1,4}(狗|人)|\d+岁"),
]


def tag_title(t):
    return [name for name, pat in TITLE_PATTERNS if re.search(pat, t or "", re.I)]


PATTERN_MIN = 30          # 近期笔记少于这么多条时，标题模式改用全部笔记


def title_patterns(sample):
    """各标题模式的条数、占比和中位赞。返回 (模式列表, 样本中位赞)。"""
    overall = statistics.median([n["likes"] for n in sample]) if sample else 0
    out = []
    for name, _ in TITLE_PATTERNS:
        hit = [n for n in sample if name in n["tags"]]
        if not hit:
            continue
        m = statistics.median([n["likes"] for n in hit])
        out.append({
            "name": name, "count": len(hit),
            "share": round(len(hit) / len(sample) * 100),
            "median": round(m), "vsOverall": round(m / overall, 2) if overall else 0,
            "top": max(hit, key=lambda x: x["likes"])["title"][:40],
        })
    out.sort(key=lambda x: -x["median"])
    return out, round(overall)


def analyze(accounts_data):
    """算标题模式表现和「值得拆解」候选。

    候选用**相对该账号自身中位数的倍数**排，不用绝对点赞——
    各账号量级差很多（中位 37 到 338），绝对值没有可比性。
    """
    notes = []
    for a in accounts_data:
        ls = [n["likes"] for n in a["notes"]] or [0]
        med = round(statistics.median(ls)) or 1
        for n in a["notes"]:
            t = n.get("time") or 0
            notes.append({**n, "account": a["nickname"], "userId": a["userId"],
                          "ratio": round(n["likes"] / med, 1), "accountMedian": med,
                          "date": (date.fromtimestamp(t / 1000).isoformat() if t else ""),
                          "tags": tag_title(n["title"])})
    if not notes:
        return {"patterns": [], "candidates": [], "overallMedian": 0, "total": 0}

    overall = statistics.median([n["likes"] for n in notes])

    # 候选必须分近期和历史：倍数最高的往往是几年前的置顶老帖，
    # 对「现在该做什么选题」没有参考价值。
    today = date.today()
    for n in notes:
        n["age"] = ((today - date.fromisoformat(n["date"])).days
                    if n.get("date") else 9999)

    # 标题模式同样只看近期，样本太少时退回全部笔记
    recent_notes = [n for n in notes if n["age"] <= RECENT_DAYS]
    sample = recent_notes if len(recent_notes) >= PATTERN_MIN else notes
    patterns, sample_median = title_patterns(sample)

    recent = sorted([n for n in notes if n["age"] <= RECENT_DAYS and n["ratio"] >= 2.5],
                    key=lambda x: -x["ratio"])[:15]
    alltime = sorted([n for n in notes if n["age"] > RECENT_DAYS and n["ratio"] >= 3.0],
                     key=lambda x: -x["ratio"])[:15]
    return {"patterns": patterns, "candidates": recent, "allTimeTop": alltime,
            "recentDays": RECENT_DAYS,
            "patternDays": RECENT_DAYS if sample is recent_notes else None,
            "patternMedian": sample_median,
            "recentCount": len([n for n in notes if n["age"] <= RECENT_DAYS]),
            "overallMedian": round(overall), "total": len(notes),
            "notes": sorted(notes, key=lambda x: -x["ratio"])}


def new_notes(user_id, notes):
    """和该账号上一次落盘的结果比，按标题找出新增的笔记。"""
    f = BENCHMARK / f"{user_id}.json"
    old = set()
    if f.exists():
        try:
            old = {n["title"] for n in json.loads(f.read_text(encoding="utf-8"))["notes"]}
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return [n for n in notes if n["title"] not in old]


def merge_notes(user_id, notes):
    """把这次读到的笔记并进该账号已有的记录。

    批量更新只读主页首屏，手动采集也未必滚到底，整份覆盖会丢掉更早的笔记。
    同一条笔记以这次的数据为准（点赞会变）；按 id 认，旧数据没有 id 时按标题认。
    """
    f = BENCHMARK / f"{user_id}.json"
    try:
        old = json.loads(f.read_text(encoding="utf-8"))["notes"] if f.exists() else []
    except (json.JSONDecodeError, OSError, KeyError):
        old = []
    ids = {n["id"] for n in notes if n.get("id")}
    titles = {n["title"] for n in notes if n.get("title")}
    kept = [n for n in old
            if not (n.get("id") and n["id"] in ids) and not (n.get("title") and n["title"] in titles)]
    return sorted(notes + kept, key=lambda n: -(n.get("time") or 0))


def save_account(user_id, r, fetched=None):
    BENCHMARK.mkdir(parents=True, exist_ok=True)
    (BENCHMARK / f"{user_id}.json").write_text(
        json.dumps({**r, "fetchedAt": fetched or date.today().isoformat(), "userId": user_id},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def write_summary(new_count):
    """重算全部对标账号的汇总供工作台读取，并记一条运行历史。

    插件是一个账号一个账号收进来的，同一天会写好几次，
    所以同一天的新增数累加，不覆盖。
    """
    data = [json.loads(f.read_text(encoding="utf-8")) for f in BENCHMARK.glob("*.json")
            if not f.name.startswith("_")]
    summary = analyze(data)
    summary["fetchedAt"] = date.today().isoformat()
    summary["accounts"] = [{"nickname": a["nickname"], "userId": a["userId"],
                            "fans": a.get("fans", ""), "desc": a.get("desc", ""),
                            "url": f"https://www.xiaohongshu.com/user/profile/{a['userId']}",
                            "noteCount": len(a["notes"]),
                            "fetchedAt": a.get("fetchedAt", ""),
                            "median": round(statistics.median(
                                [n["likes"] for n in a["notes"]] or [0]))}
                           for a in data]
    (BENCHMARK / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    hist_f = BENCHMARK / "_history.json"
    hist = []
    if hist_f.exists():
        try:
            hist = json.loads(hist_f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            hist = []
    prev = next((h for h in hist if h.get("date") == summary["fetchedAt"]), None)
    hist = [h for h in hist if h is not prev]
    hist.append({"date": summary["fetchedAt"], "total": summary["total"],
                 "new": new_count + (prev or {}).get("new", 0),
                 "overallMedian": summary["overallMedian"]})
    hist_f.write_text(json.dumps(hist[-60:], ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return summary
