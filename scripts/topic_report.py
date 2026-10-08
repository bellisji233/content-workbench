#!/usr/bin/env python3
"""选题分析报告 —— 基于自有账号数据，跑出哪些选题方向值得继续做。

用法:  python3 scripts/topic_report.py
输出:  local/data/topic-reports/{今天}-自有内容.md

每次放入新的平台导出后跑一次；本地工作台开着时会自动重算。每份报告独立留档，
工作台里能看到历次报告，所以不覆盖旧文件（同一天重跑覆盖当天那份）。
分类口径在 classify.py，判定阈值在 local/config.toml 的 [report]。
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_workbench as bw                               # noqa: E402
from build_workbench import build                       # noqa: E402
from classify import classify, group_by_type, summarize  # noqa: E402
from paths import CONFIG, EXPORTS, TOPIC_REPORTS as OUTDIR  # noqa: E402

TODAY = date.today().isoformat()

# 判定阈值在 local/config.toml 的 [report]；拿全账号中位数当基线，避免用绝对值给不同量级的平台打分
_R = CONFIG.get("report", {})
STRONG_FAN = _R.get("strong_fan", 3.0)     # 涨粉 ‰
WEAK_FAN = _R.get("weak_fan", 1.0)
MIN_SAMPLES = _R.get("min_samples", 3)


def verdict(s, base_fan):
    """给一个选题类型下判断。样本太少只观察，不下结论。"""
    if s["n"] < MIN_SAMPLES:
        return "样本不足", f"只有 {s['n']} 条，再攒几条再评"
    if s["fanRate"] >= STRONG_FAN:
        return "✅ 主力方向", "涨粉效率显著高于账号均值，应该继续加码"
    if s["fanRate"] <= WEAK_FAN:
        if s["median"] >= base_fan["median"]:
            return "⚠️ 有量无转化", "播放不差但换不来关注，做曝光可以，别指望涨粉"
        return "⚠️ 降权", "播放和转化都在均值以下"
    return "◐ 中性", "表现接近均值，看具体选题而不是类型"


def cell(x):
    """标题里出现 | 会把表格切多一格，换成全角竖线。"""
    return str(x).replace("|", "｜").replace("\n", " ")


def fmt_table(headers, rows, align=None):
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(x) for x in r) + " |")
    return out


def build_report():
    items, pubs, orphans, dupes, platforms = build()
    stats = [(pf["name"], summarize(pf["rows"])) for pf in platforms if pf["rows"]]
    allp = pubs          # 全部发布记录；按稿件汇总会把两条产线都认领的同一条算两次
    overall = summarize(allp)
    groups = group_by_type(allp)
    ranked = sorted(groups.items(), key=lambda kv: -summarize(kv[1])["fanRate"])

    L = []
    L.append(f"# 选题分析报告 · {TODAY}")
    L.append("")
    L.append("> 基于自有账号已发布数据，评估各选题方向的真实表现。")
    def scope(pf):
        if not pf["path"]:
            return f"{pf['name']} 0 条"
        n = len(bw.platform_exports(next(x for x in bw.PLATFORMS if x["name"] == pf["name"])))
        latest = pf["path"].relative_to(EXPORTS).with_suffix("")
        return f"{pf['name']} {len(pf['rows'])} 条（合并 {n} 份导出，最新 {latest}）" if n > 1 \
            else f"{pf['name']} {len(pf['rows'])} 条（{latest}）"
    src = " + ".join(scope(pf) for pf in platforms)
    L.append(f"> 数据口径：{src}，合计 {len(allp)} 条。")
    L.append("")
    L.append(f"账号整体：总播放 {overall['views']:,} · 收藏率 {overall['collectRate']:.1f}%"
             f" · 涨粉 {overall['fanRate']:.2f}‰ · 累计涨粉 {overall['fans']}")
    L.append("")

    # ---------- 1 结论先行 ----------
    L.append("## 1. 结论")
    L.append("")
    # 只有样本量够的类型才有资格当主力方向——1 条内容的高转化是噪声不是信号
    solid = [(k, v) for k, v in ranked if summarize(v)["n"] >= MIN_SAMPLES]
    top = solid[0] if solid else ranked[0]
    ts = summarize(top[1])
    share = ts["fans"] / overall["fans"] * 100 if overall["fans"] else 0
    L.append(f"**主力方向是「{top[0]}」。** {ts['n']} 条内容贡献了 {ts['fans']} 个新粉，"
             f"占账号总涨粉的 {share:.0f}%，涨粉效率 {ts['fanRate']:.2f}‰。")
    L.append("")

    weak = [(k, summarize(v)) for k, v in ranked
            if summarize(v)["n"] >= MIN_SAMPLES and summarize(v)["fanRate"] <= WEAK_FAN]
    for k, s in weak:
        if s["median"] >= overall["median"]:
            L.append(f"**「{k}」是有量无转化的典型。** 播放中位 {s['median']:,}"
                     f"（账号中位 {overall['median']:,}），但涨粉只有 {s['fanRate']:.2f}‰。"
                     f"能拉曝光，换不来关注。")
        else:
            L.append(f"**「{k}」两头都弱。** 播放中位 {s['median']:,}、"
                     f"涨粉 {s['fanRate']:.2f}‰，都在均值以下。")
        L.append("")

    # ---------- 2 分类型总表 ----------
    L.append("## 2. 各选题方向表现")
    L.append("")
    L.append("按涨粉效率排序。**涨粉/千播**是转化效率，**收藏率**是沉淀意愿——"
             "两个都比绝对播放量更能说明选题好坏。")
    L.append("")
    rows = []
    for k, v in ranked:
        s = summarize(v)
        verd, _ = verdict(s, overall)
        rows.append([k, s["n"], f"{s['median']:,}", f"{s['collectRate']:.1f}%",
                     f"{s['fanRate']:.2f}‰", s["fans"], verd])
    L += fmt_table(["选题方向", "条数", "播放中位", "收藏率", "涨粉/千播", "累计涨粉", "判定"],
                   rows, ["---", "--:", "--:", "--:", "--:", "--:", "---"])
    L.append("")

    # ---------- 3 平台分化 ----------
    L.append("## 3. 平台分化")
    L.append("")
    if len(stats) >= 2:
        by_views = sorted(stats, key=lambda x: -x[1]["views"])
        by_fan = max(stats, key=lambda x: x[1]["fanRate"])
        (vn, vs), (n2, s2) = by_views[0], by_views[1]
        if by_fan[0] != vn:
            fn, fs = by_fan
            L.append(f"- **{vn}给量**：总播放 {vs['views']:,}，是{fn if len(stats) == 2 else n2}的 "
                     f"{vs['views'] / (fs if len(stats) == 2 else s2)['views']:.1f} 倍；"
                     f"但涨粉只有 {vs['fanRate']:.2f}‰")
            L.append(f"- **{fn}给沉淀**：播放中位仅 {fs['median']:,}，"
                     f"但收藏率 {fs['collectRate']:.1f}%、涨粉 {fs['fanRate']:.2f}‰")
            L.append("")
            L.append(f"同一个选题在不同平台的角色不同：{vn}负责被看见，{fn}负责被留下。"
                     "**别用一套结论指导所有平台。**" if len(stats) > 2 else
                     f"同一个选题在两个平台的角色不同：{vn}负责被看见，{fn}负责被留下。"
                     "**别用一套结论指导两个平台。**")
        else:
            L.append(f"- **{vn}播放和涨粉效率都领先**：总播放 {vs['views']:,}，"
                     f"涨粉 {vs['fanRate']:.2f}‰")
        L.append("")
    for plat, arr in ((pf["name"], pf["rows"]) for pf in platforms if pf["rows"]):
        L.append(f"### {plat}")
        L.append("")
        g = group_by_type(arr)
        rr = sorted(g.items(), key=lambda kv: -summarize(kv[1])["fanRate"])
        L += fmt_table(["选题方向", "条数", "播放中位", "收藏率", "涨粉/千播"],
                       [[k, summarize(v)["n"], f"{summarize(v)['median']:,}",
                         f"{summarize(v)['collectRate']:.1f}%",
                         f"{summarize(v)['fanRate']:.2f}‰"] for k, v in rr],
                       ["---", "--:", "--:", "--:", "--:"])
        L.append("")

    # ---------- 4 单条极值 ----------
    L.append("## 4. 值得复制的与值得警惕的")
    L.append("")
    sized = [p for p in allp if p["views"] >= 500]
    best = sorted(sized, key=lambda p: p["fans"] / p["views"] * 1000, reverse=True)[:6]
    L.append("**转化最高的 6 条**（播放 ≥500）：")
    L.append("")
    L += fmt_table(["内容", "平台", "类型", "播放", "涨粉", "涨粉/千播"],
                   [[p["title"][:30], p["platform"], classify(p["title"]),
                     f"{p['views']:,}", p["fans"], f"{p['fans']/p['views']*1000:.2f}‰"]
                    for p in best], ["---", "---", "---", "--:", "--:", "--:"])
    L.append("")
    trap = sorted([p for p in sized if p["views"] >= 2000],
                  key=lambda p: p["fans"] / p["views"])[:4]
    L.append("**高播放低转化**（播放 ≥2000 但换不来粉，做之前想清楚目的）：")
    L.append("")
    L += fmt_table(["内容", "平台", "类型", "播放", "涨粉"],
                   [[p["title"][:30], p["platform"], classify(p["title"]),
                     f"{p['views']:,}", p["fans"]] for p in trap],
                   ["---", "---", "---", "--:", "--:"])
    L.append("")

    # ---------- 5 下一步 ----------
    L.append("## 5. 下一步选题建议")
    L.append("")
    tips = [f"**加码「{top[0]}」**：涨粉效率 {ts['fanRate']:.2f}‰，"
            f"贡献了 {share:.0f}% 的新粉，下一批选题里应占多数。"]
    if weak:
        names = "、".join(f"「{k}」" for k, _ in weak)
        tips.append(f"**{names}要么减产，要么改目的**：当曝光工具可以，"
                    f"但要在里面绑定「关注能持续得到什么」，否则流量白过。")
    ranked_fan = sorted(stats, key=lambda x: -x[1]["fanRate"])
    if len(ranked_fan) >= 2 and ranked_fan[-1][1]["fanRate"]:
        (bn, bs), (wn, ws) = ranked_fan[0], ranked_fan[-1]
        tips.append(f"**{bn}优先**：涨粉效率是{wn}的 "
                    f"{bs['fanRate']/ws['fanRate']:.1f} 倍。同样的稿子，"
                    f"{bn}那版值得多花时间打磨标题和封面。")
    thin = [k for k, v in ranked if summarize(v)["n"] < MIN_SAMPLES]
    if thin:
        tips.append(f"**{'、'.join(f'「{k}」' for k in thin)} 样本还不够**（各不足 3 条），"
                    f"想评估就再补几条，别拿单条表现下结论。")
    for i, t in enumerate(tips, 1):
        L.append(f"{i}. {t}")
    L.append("")

    # ---------- 6 与上一份的变化 ----------
    prev = sorted(OUTDIR.glob("*-自有内容.md")) if OUTDIR.exists() else []
    prev = [p for p in prev if not p.name.startswith(TODAY)]
    L.append("## 6. 与上一份报告的变化")
    L.append("")
    if prev:
        L.append(f"上一份：`{prev[-1].name}`。逐项对比需人工过一遍——"
                 f"重点看主力方向有没有换、判定为「降权」的类型是否真的减产了。")
    else:
        L.append("这是第一份报告，没有可比对象。下次跑的时候这里会填变化。")
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*由 `scripts/topic_report.py` 于 {TODAY} 生成。"
             f"分类口径见 `scripts/classify.py`，改规则后重跑本脚本。*")
    return "\n".join(L)


if __name__ == "__main__":
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / f"{TODAY}-自有内容.md"
    out.write_text(build_report(), encoding="utf-8")
    print(f"✓ {out}")
    print(f"  历史报告 {len(list(OUTDIR.glob('*.md')))} 份")
    print("  重跑看板即可在「选题报告」里读到：python3 scripts/build_workbench.py")
