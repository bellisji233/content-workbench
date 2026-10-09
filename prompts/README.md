# 分析提示词

页面和 agent 共用前三份；`classify/` 里的两份由本地服务和 `classify.py` 调用，给作品划分选题方向。`{{名称}}` 是占位符，用的时候换成对应内容。

| 文件 | 用途 | 占位符 |
|---|---|---|
| `single.md` | 单篇爆款拆解 | `title` `author` `type` `publishedAt` `likes` `collects` `comments` `tags` `content` `topComments` `transcriptNote` |
| `multi.md` | 多篇结构共性分析 | `count` `samples` |
| `topics.md` | 选题推荐 | `accountName` `accountPositioning` `accountBio` `today` `myReport` `myDirections` `myRecent` `myWip` `myAssets` `recentDays` `candidates` `latest` `patternDays` `patterns` |
| `classify/partition.md` | 划分选题方向，并给全部作品归类 | `accountName` `accountPositioning` `count` `titles` `seed` `hint` |
| `classify/assign.md` | 把新作品归入已有方向 | `categories` `titles` |

占位符的内容从采集箱的笔记文件（`local/data/inbox/*.json`）和对标汇总（`local/data/benchmark/_summary.json`）里取：

- `content`：有口播转写时为「【口播全文】（N 句转写，N 字）+ 全文 +【笔记简介】+ 简介」，否则为「【正文】+ 正文」
- `topComments`：每条一行，「- 作者（N赞）：评论」
- `transcriptNote`：视频笔记还没转写时，提醒「凡是需要口播才能下的判断，请标注需转写后才能判断」
- `samples`：每篇一段，含标题、体裁、互动、话题、口播或正文前 2500 字、前 6 条评论，段与段之间用 `---` 分隔
- `accountName` / `accountBio`：`local/config.toml` 的 `[account]`
- `myReport`：最新一份自有内容分析报告（前 4000 字）
- `myDirections`：选题方向，每行「- 方向（N 条）：说明」
- `myRecent`：最近发布的 25 条，每行「- 日期 · 平台 · 方向 · 标题 · 播放 · 涨粉」
- `myWip`：生产中、待发布的内容标题，没有时为「（无）」
- `myAssets`：爆款库里评为优先的写法
- `candidates` / `patterns`：对标汇总里的近期高倍数笔记（前 10 条）和标题模式
- `titles`：每行「编号 · 平台 · 标题」，标题超过 150 字截断
- `seed`：重新划分时附上现有方向，「合适的沿用原名」；第一次划分为空
- `hint`：用户在页面上写的划分要求，没有时为空

页面上的拼装逻辑在 `templates/workbench-app.html` 的 `promptSingle` / `promptMulti` / `promptTopics`，改格式时两边一起改。
