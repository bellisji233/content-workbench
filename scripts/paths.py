"""工作台用到的所有路径，集中在这一处。

代码（scripts/、templates/、clipper/）和只属于本账号的东西（local/）分开放：
  local/config.toml   外部路径（流水线项目、Obsidian、挖掘项目、下载目录）与线上地址
  local/*.md          手工维护的配置：状态覆盖表、对标账号、收藏夹过滤规则
  local/data/         运行中产生的数据
  local/out/          生成的工作台页面
"""

import os
import sys

if sys.version_info < (3, 11):
    sys.exit(f"工作台需要 Python 3.11 或更高版本，当前是 {sys.version.split()[0]}。\n"
             "macOS 自带的 python3 是 3.9：用 Homebrew 安装（brew install python），或从 python.org 下载。")

import tomllib  # noqa: E402
from pathlib import Path

WB = Path(__file__).resolve().parent.parent
# WORKBENCH_LOCAL 指向另一份 local/（示例数据就是这样跑的），不设就用 local/
LOCAL = Path(os.environ.get("WORKBENCH_LOCAL") or WB / "local").resolve()

_cfg_file = LOCAL / "config.toml"
CONFIG = tomllib.loads(_cfg_file.read_text(encoding="utf-8")) if _cfg_file.exists() else {}
_cfg = CONFIG
_paths = _cfg.get("paths", {})


def _path(key, default):
    v = _paths.get(key, default)
    if v is None or v == "":
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else (LOCAL / p).resolve()


# 外部：流水线项目与其他项目
PROJECT = _path("project", "../..")
# 流水线文件夹：[pipeline] path 直接写路径；旧写法是 paths.project 下的 dir
_pipe = _cfg.get("pipeline")
WORKS = None
if _pipe is not None:
    _p = _pipe.get("path")
    WORKS = ((Path(_p).expanduser() if Path(_p).expanduser().is_absolute() else (LOCAL / _p).resolve())
             if _p else PROJECT / _pipe.get("dir", "works"))
EXPORTS = _path("exports", None) or PROJECT / "data" / "xhs-exports"
DRAFTS = _path("drafts", None)
# 拆解项目（可选）：有就读它的拆解报告并把爆款库写进它的资产总库；没有就用本地爆款库
MINING = _path("mining", None)
VAULT = MINING / "work" / "_资产总库.md" if MINING else LOCAL / "data" / "爆款库.md"
DOWNLOAD_INBOX = _path("download_inbox", "~/Downloads/xhs-inbox")
DOWNLOAD_ANALYSES = _path("download_analyses", "~/Downloads/xhs-analyses")
WORKBENCH_URL = _cfg.get("workbench", {}).get("url", "")
# 本地工作台的分析方式：engine = claude（本机 Claude Code）或 command（任意命令行）
ANALYSIS = _cfg.get("analysis", {})
ANALYSIS_MODEL = ANALYSIS.get("model", "")
# 本地分析调用 claude -p 时的工作目录：放在项目之外，避免把项目 CLAUDE.md 带进分析
AGENT_CWD = Path.home() / ".content-factory" / "workbench-agent"

# 本账号：手工配置
STATUS = LOCAL / "status.md"

# 本账号：运行数据
DATA = LOCAL / "data"
INBOX = DATA / "inbox"
BENCHMARK = DATA / "benchmark"
ANALYSES = DATA / "analyses"
TOPIC_REPORTS = DATA / "topic-reports"
LOGS = DATA / "logs"
# 本地工作台的状态与分析，结构和线上数据库一致：state/{collection}/{id}.json
STATE = DATA / "state"

# 页面
TEMPLATE = WB / "templates" / "workbench-app.html"
PROMPTS = WB / "prompts"
OUT_HTML = LOCAL / "out" / "workbench.html"
OUT_JSON = LOCAL / "out" / "workbench-data.json"
