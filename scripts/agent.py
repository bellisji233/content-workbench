"""调用本机 agent：页面上的分析（流式）和后台任务（选题分类，一次拿全文）共用同一套配置。

local/config.toml 的 [analysis]：
  engine = "claude"    本机 Claude Code（默认）
  engine = "command"   任意命令行，提示词从标准输入进，结果从标准输出出
"""

import shlex
import shutil
import subprocess
from pathlib import Path

from paths import AGENT_CWD, ANALYSIS, ANALYSIS_MODEL

SYSTEM_PROMPT = "直接输出用户要求的内容，使用 Markdown。不要开场白，不要结束语。"


class AgentError(Exception):
    """agent 没装、调用失败或超时。消息可以直接给用户看。"""


def claude_exe():
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def claude_cmd(stream=True, system=SYSTEM_PROMPT):
    cmd = [claude_exe(), "-p", "--tools", "", "--no-session-persistence",
           "--strict-mcp-config", "--system-prompt", system]
    if stream:
        cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if ANALYSIS_MODEL:
        cmd += ["--model", ANALYSIS_MODEL]
    return cmd


def analysis_cmd(stream=True, system=SYSTEM_PROMPT):
    """返回 (命令, 输出格式)。claude 流式时输出 stream-json，其余都是纯文本。"""
    if ANALYSIS.get("engine") == "command" and ANALYSIS.get("command"):
        return shlex.split(ANALYSIS["command"]), "text"
    return claude_cmd(stream, system), "claude" if stream else "text"


def available():
    cmd, _ = analysis_cmd(stream=False)
    return bool(shutil.which(cmd[0]) or Path(cmd[0]).exists())


def missing_message(cmd):
    name = Path(cmd[0]).name
    return ("本机没有找到 Claude Code，无法分析。安装后重试；使用其他 agent 时，让 agent 把分析方式改成它的命令"
            if name == "claude" else f"没有找到分析命令 {name}，无法分析。确认已安装，或让 agent 检查分析方式的配置")


def run(prompt, system="只输出用户要求的内容，不要开场白，不要结束语。", timeout=900):
    """一次调用，返回全文。"""
    AGENT_CWD.mkdir(parents=True, exist_ok=True)
    cmd, _ = analysis_cmd(stream=False, system=system)
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), cwd=AGENT_CWD,
                           capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise AgentError(missing_message(cmd))
    except subprocess.TimeoutExpired:
        raise AgentError(f"分析超过 {timeout // 60} 分钟没有返回")
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not out:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AgentError(err[-1] if err else out or f"分析命令退出码 {r.returncode}")
    return out
