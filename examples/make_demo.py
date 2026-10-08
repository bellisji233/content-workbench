#!/usr/bin/env python3
"""生成示例数据：不导入任何东西，也能看到一个完整的工作台。

用法:  python3 examples/make_demo.py      生成到 examples/demo/
       python3 scripts/serve.py --demo    打开示例工作台（没生成时会自动生成）

账号、标题、数据全部虚构，日期按今天往前推，每次生成都是「最近」的数据。
"""

import json
import os
import random
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "demo"
LOCAL = DEMO / "local"
PROJECT = DEMO / "project"
TODAY = date.today()
rng = random.Random(7)

CONFIG = '''# 示例工作台配置：路径都指向 examples/demo/ 里的虚构数据
[paths]
project = "../project"
exports = "../project/exports"
drafts = "../project/drafts"
download_inbox = "../downloads/inbox"
download_analyses = "../downloads/analyses"

[workbench]
url = ""

[analysis]
engine = "claude"
model = ""
command = ""

[account]
name = "示例·效率研究所"
bio = "用 AI 工具做内容和小产品，分享可以照着做的教程和工作流。"

[status]
extra = []

[[platforms]]
name = "小红书"
dir = "redbook"
[platforms.columns]
title = "笔记标题"
date = "首次发布时间"
form = "体裁"
impressions = "曝光"
views = "观看量"
ctr = "封面点击率"
likes = "点赞"
comments = "评论"
collects = "收藏"
fans = "涨粉"
shares = "分享"

[[platforms]]
name = "抖音"
dir = "douyin"
[platforms.columns]
title = "作品名称"
date = "发布时间"
form = "体裁"
impressions = "播放量"
views = "播放量"
finish5s = "5s完播率"
bounce2s = "2s跳出率"
likes = "点赞量"
shares = "分享量"
comments = "评论量"
collects = "收藏量"
fans = "粉丝增量"

[pipeline]
dir = "works"
junk = []

[[pipeline.stages]]
key = "选"
label = "选题"
files = ["topic.md"]

[[pipeline.stages]]
key = "稿"
label = "文稿"
files = ["draft.md"]

[[pipeline.stages]]
key = "剪"
label = "剪辑"
files = ["*.mp4"]

[[pipeline.stages]]
key = "封"
label = "封面"
files = ["cover.png"]

[[pipeline.stages]]
key = "发"
label = "发布"
published = true

[drafts]
done = ["选", "稿"]

[report]
strong_fan = 3.0
weak_fan = 1.0
min_samples = 3

[[categories]]
name = "工具教程"
keywords = ["教程", "上手", "手把手", "保姆级"]

[[categories]]
name = "工作流分享"
keywords = ["工作流", "自动化", "搭了", "系统"]

[[categories]]
name = "产品测评"
keywords = ["测评", "对比", "实测", "横评"]

[[categories]]
name = "观点随笔"
keywords = ["聊聊", "思考", "为什么"]
'''

# (标题, 体裁, 播放量级, 涨粉率‰, 收藏率%)
POSTS = [
    ("保姆级教程：用 AI 十分钟搭一个个人网站", "视频", 9000, 6.0, 4.0),
    ("手把手教你把会议录音变成待办清单", "视频", 4200, 5.2, 5.5),
    ("新手上手 AI 剪辑，这 5 个设置先改掉", "图文", 2600, 4.4, 6.0),
    ("我搭了一套选题自动化工作流，每周省 6 小时", "视频", 7000, 4.8, 3.2),
    ("用 AI 做读书笔记的完整工作流", "图文", 1800, 3.6, 7.5),
    ("自动化整理收藏夹：我的三步系统", "视频", 3100, 3.9, 4.1),
    ("三款 AI 笔记工具实测对比，结论有点意外", "视频", 12000, 1.2, 1.6),
    ("五个 AI 绘图工具横评：谁最适合做封面", "图文", 6500, 0.9, 2.2),
    ("国产大模型写作能力测评", "视频", 8800, 0.7, 1.1),
    ("聊聊为什么我不再追每一个新模型", "视频", 5200, 0.8, 0.9),
    ("一点思考：工具越多，产出越少？", "图文", 900, 1.1, 1.8),
    ("给自己做了个记账小程序", "视频", 2300, 2.6, 2.9),
]


def ts(d):
    return datetime(d.year, d.month, d.day, 20, tzinfo=timezone.utc)


def write_exports():
    import openpyxl
    posts = []
    for i, (title, form, views, fan, col) in enumerate(POSTS):
        d = TODAY - timedelta(days=4 + i * 6)
        for plat, scale in (("小红书", 0.35), ("抖音", 1.6)):
            v = int(views * scale * rng.uniform(0.8, 1.2))
            posts.append({"plat": plat, "title": title, "date": d, "form": form, "views": v,
                          "likes": int(v * rng.uniform(0.02, 0.05)),
                          "collects": int(v * col / 100 * (1.6 if plat == "小红书" else 0.6)),
                          "comments": int(v * rng.uniform(0.002, 0.006)),
                          "fans": int(v * fan / 1000 * (1.8 if plat == "小红书" else 0.6)),
                          "shares": int(v * rng.uniform(0.002, 0.008))})

    rb = openpyxl.Workbook().active
    rb.append(["最多导出排序后前1000条笔记"] * 11)
    rb.append(["笔记标题", "首次发布时间", "体裁", "曝光", "观看量", "封面点击率", "点赞",
               "评论", "收藏", "涨粉", "分享"])
    for p in posts:
        if p["plat"] == "小红书":
            rb.append([p["title"], p["date"].strftime("%Y年%m月%d日20时"), p["form"],
                       p["views"] * 9, p["views"], round(rng.uniform(0.04, 0.12), 3), p["likes"],
                       p["comments"], p["collects"], p["fans"], p["shares"]])
    out = PROJECT / "exports" / "redbook" / TODAY.isoformat() / "笔记列表明细表.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    rb.parent.save(out)

    dy = openpyxl.Workbook().active
    dy.append(["作品名称", "发布时间", "体裁", "审核状态", "播放量", "完播率", "5s完播率",
               "封面点击率", "2s跳出率", "平均播放时长", "点赞量", "分享量", "评论量", "收藏量",
               "主页访问量", "粉丝增量"])
    for p in posts:
        if p["plat"] == "抖音":
            dy.append([p["title"], p["date"].strftime("%Y-%m-%d 20:00"), "1-3min视频", "公开",
                       str(p["views"]), "0.01", f"{rng.uniform(0.18, 0.35):.6f}", "-",
                       f"{rng.uniform(0.35, 0.65):.6f}", "6.5", str(p["likes"]), str(p["shares"]),
                       str(p["comments"]), str(p["collects"]), "40", str(p["fans"])])
    out = PROJECT / "exports" / "douyin" / f"{TODAY.isoformat()}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    dy.parent.save(out)


def write_works():
    # (发布标题, 已完成的阶段文件)
    works = [
        ("保姆级教程：用 AI 十分钟搭一个个人网站", ["topic.md", "draft.md", "final.mp4", "cover.png"]),
        ("我搭了一套选题自动化工作流，每周省 6 小时", ["topic.md", "draft.md", "final.mp4", "cover.png"]),
        ("三款 AI 笔记工具实测对比，结论有点意外", ["topic.md", "draft.md", "final.mp4"]),
        ("手机端 AI 修图工具横评", ["topic.md", "draft.md"]),
        ("用 AI 给播客做章节和金句卡", ["topic.md"]),
    ]
    for i, (title, files) in enumerate(works):
        d = TODAY - timedelta(days=4 + i * 6) if i < 3 else TODAY - timedelta(days=2 + i)
        folder = PROJECT / "works" / f"{d.isoformat()}-{title}"
        folder.mkdir(parents=True, exist_ok=True)
        for f in files:
            (folder / f).write_text(f"# {title}\n\n示例文件。\n", encoding="utf-8")


def write_drafts():
    drafts = [
        "手把手教你把会议录音变成待办清单", "用 AI 做读书笔记的完整工作流",
        "聊聊为什么我不再追每一个新模型", "给自己做了个记账小程序", "草稿 下周想写的 AI 日程助手",
    ]
    d = PROJECT / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(drafts):
        name = t if t.startswith("草稿") else f"{(TODAY - timedelta(days=10 + i * 5)).isoformat()} {t}"
        (d / f"{name}.md").write_text(f"# {t.removeprefix('草稿 ')}\n\n" + "示例口播稿正文。" * 20,
                                      encoding="utf-8")


def write_inbox():
    d = LOCAL / "data" / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    notes = [
        {"noteId": "demo0000000000000001", "type": "视频", "title": "零基础用 AI 做一个待办小程序，全程实录",
         "author": {"name": "示例·小橘子做工具"}, "stats": {"likes": 8200, "collects": 6100, "comments": 420},
         "tags": ["AI编程", "效率工具", "教程"], "body": "全程录屏，从零到上线。",
         "transcript": [{"start": 0, "end": 4, "text": "今天用一个小时，从零做一个待办小程序。"}],
         "transcriptText": "今天用一个小时，从零做一个待办小程序。先说结论：不会写代码也能做出来。"},
        {"noteId": "demo0000000000000002", "type": "图文", "title": "我把 20 个 AI 工具精简到 3 个，原因在这",
         "author": {"name": "示例·晨间效率"}, "stats": {"likes": 3100, "collects": 4500, "comments": 260},
         "tags": ["AI工具", "效率"], "body": "工具越多越乱。留下的三个分别负责：记录、写作、检索。"},
    ]
    for i, n in enumerate(notes):
        n.update({"capturedAt": ts(TODAY - timedelta(days=i + 1)).isoformat(),
                  "publishedAt": ts(TODAY - timedelta(days=i + 6)).isoformat(),
                  "url": f"https://www.xiaohongshu.com/explore/{n['noteId']}", "source": "__INITIAL_STATE__",
                  "comments": [{"author": "示例读者", "text": "收藏了，周末试试", "likes": 52}],
                  "images": [], "video": None, "warnings": []})
        (d / f"{n['noteId']}.json").write_text(json.dumps(n, ensure_ascii=False, indent=2),
                                               encoding="utf-8")


def write_benchmark():
    import benchmark
    accounts = [("demo_u1", "示例·小橘子做工具", 60), ("demo_u2", "示例·晨间效率", 20),
                ("demo_u3", "示例·设计周记", 45)]
    titles = ["教程：{} 从 0 到 1", "{} 真的好用吗？实测一周", "我用 {} 搭了一个自动化系统",
              "3 个让 {} 更好用的设置", "为什么我最后选了 {}", "{} 新手上手指南"]
    tools = ["AI 剪辑", "AI 笔记", "AI 绘图", "智能体", "AI 搜索", "提示词"]
    for uid, nick, base in accounts:
        notes = []
        for j in range(12):
            likes = int(base * rng.uniform(0.4, 2.0) * (6 if j in (1, 4) else 1))
            notes.append({"token": "", "title": rng.choice(titles).format(rng.choice(tools)) + f"（{j + 1}）",
                          "type": "video" if j % 2 else "normal",
                          "time": int(ts(TODAY - timedelta(days=5 + j * 9)).timestamp() * 1000),
                          "likes": likes, "collects": int(likes * 0.6), "cover": ""})
        benchmark.save_account(uid, {"ok": True, "nickname": nick, "desc": "示例账号，数据为虚构",
                                     "fans": f"{rng.randint(2, 9)}万", "notes": notes, "via": "示例"},
                               fetched=(TODAY - timedelta(days=rng.randint(1, 20))).isoformat())
    benchmark.write_summary(0)


VAULT = """# 爆款库

## 开场/钩子

| 资产 | 来源 | 使用记录 | 评级 |
|---|---|---|---|
| 结果先行开场「先看成品，再讲怎么做」 | 示例拆解 · 个人网站教程 | 《三款 AI 笔记工具实测对比，结论有点意外》08-29：5s完播 1.01 倍、收藏率 0.5 倍【平】；《我搭了一套选题自动化工作流，每周省 6 小时》09-16：5s完播 1.21 倍、2s跳出 0.74 倍【优】 | 待验证（2 次：优 1 / 平 1 / 弱 0） |
| 痛点提问开场「你是不是也…」 | 示例拆解 · 会议纪要 | 未回流 | 待验证 |

## 选题

| 资产 | 来源 | 使用记录 | 评级 |
|---|---|---|---|
| 保姆级教程类（一步一截图，照做就能复现） | 示例拆解 · 个人网站教程 | 《手把手教你把会议录音变成待办清单》09-28：收藏率 1.8 倍、涨粉/千播 1.7 倍【优】；《用 AI 做读书笔记的完整工作流》09-10：收藏率 2.5 倍【优】 | ✅优先 |
| 多工具横评「实测对比，结论意外」 | 示例拆解 · 工具测评 | 《三款 AI 笔记工具实测对比，结论有点意外》08-29：播放 3 倍，但收藏率、涨粉/千播约 0.5 倍【弱】 | 待验证（1 次：优 0 / 平 0 / 弱 1） |

## 论证/结构

| 资产 | 来源 | 使用记录 | 评级 |
|---|---|---|---|
| 问题 → 演示 → 可复制的模板 | 示例拆解 · 会议纪要 | 未回流 | 待验证 |

## 话术/金句

| 资产 | 来源 | 使用记录 | 评级 |
|---|---|---|---|
| 量化收益「每周省 N 小时」 | 示例拆解 · 选题工作流 | 未回流 | 待验证 |

## 收尾/CTA

| 资产 | 来源 | 使用记录 | 评级 |
|---|---|---|---|
| 收尾给模板「评论区回复关键词领模板」 | 示例拆解 · 读书笔记 | 未回流 | 待验证 |
"""


def write_vault():
    d = LOCAL / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "爆款库.md").write_text(VAULT, encoding="utf-8")


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    LOCAL.mkdir(parents=True)
    (LOCAL / "config.toml").write_text(CONFIG, encoding="utf-8")
    shutil.copy(HERE.parent / "local.example" / "status.md", LOCAL / "status.md")
    (DEMO / "downloads" / "inbox").mkdir(parents=True)
    (DEMO / "downloads" / "analyses").mkdir(parents=True)
    os.environ["WORKBENCH_LOCAL"] = str(LOCAL)
    sys.path.insert(0, str(HERE.parent / "scripts"))
    write_exports()
    write_works()
    write_drafts()
    write_inbox()
    write_benchmark()
    write_vault()
    print(f"✓ 示例数据：{DEMO}")
    print("  打开：python3 scripts/serve.py --demo")


if __name__ == "__main__":
    main()
