# 分析提示词

页面和 agent 共用这三份。`{{名称}}` 是占位符，用的时候换成对应内容。

| 文件 | 用途 | 占位符 |
|---|---|---|
| `single.md` | 单篇爆款拆解 | `title` `author` `type` `publishedAt` `likes` `collects` `comments` `tags` `content` `topComments` `transcriptNote` |
| `multi.md` | 多篇结构共性分析 | `count` `samples` |
| `topics.md` | 选题推荐 | `accountName` `accountPositioning` `accountBio` `today` `myReport` `recentDays` `candidates` `latest` `patternDays` `patterns` |

占位符的内容从采集箱的笔记文件（`local/data/inbox/*.json`）和对标汇总（`local/data/benchmark/_summary.json`）里取：

- `content`：有口播转写时为「【口播全文】（N 句转写，N 字）+ 全文 +【笔记简介】+ 简介」，否则为「【正文】+ 正文」
- `topComments`：每条一行，「- 作者（N赞）：评论」
- `transcriptNote`：视频笔记还没转写时，提醒「凡是需要口播才能下的判断，请标注需转写后才能判断」
- `samples`：每篇一段，含标题、体裁、互动、话题、口播或正文前 2500 字、前 6 条评论，段与段之间用 `---` 分隔
- `accountName` / `accountBio`：`local/config.toml` 的 `[account]`
- `myReport`：最新一份选题报告的前 1800 字
- `candidates` / `patterns`：对标汇总里的近期高倍数笔记（前 10 条）和标题模式

页面上的拼装逻辑在 `templates/workbench-app.html` 的 `promptSingle` / `promptMulti` / `promptTopics`，改格式时两边一起改。
