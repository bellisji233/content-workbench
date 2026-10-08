"""选题类型分类 —— 报告与看板共用同一套口径。

规则在 local/config.toml 的 [[categories]]，按顺序匹配标题，第一个命中即归类。
调整规则后两边同时生效，不会出现「报告说 A、看板说 B」。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CONFIG  # noqa: E402


def _rules():
    out = []
    for c in CONFIG.get("categories", []):
        if c.get("pattern"):
            out.append((c["name"], c["pattern"]))
        elif c.get("keywords"):
            out.append((c["name"], "|".join(re.escape(k) for k in c["keywords"])))
    return out


RULES = _rules()

OTHER = "其他"


def classify(title):
    for name, pat in RULES:
        if re.search(pat, title or "", re.I):
            return name
    return OTHER


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
