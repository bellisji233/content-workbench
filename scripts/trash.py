"""页面上的删除与撤销。

删除不直接抹掉文件：移进 local/data/trash/{token}/，同目录记一份 meta.json（删了什么、原来在哪），
页面上点「撤销」就按 meta 搬回去。回收站里超过 30 天的条目在下一次删除时清掉。

四类东西可删：
  inbox         采集箱笔记：笔记 JSON 和转写用的音频
  bench         对标账号：账号数据，并记进 benchmark/_removed.json，
                插件批量更新再交来这个账号时不收（手动抓取主页则视为重新加入）
  topic-report  自有内容分析报告
  report        拆解项目里的历史报告：文件不归工作台管，只从列表里隐藏（记在 hidden.json）
"""

import json
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from paths import BENCHMARK, HIDDEN, INBOX, TOPIC_REPORTS, TRASH

KEEP_DAYS = 30
REMOVED = BENCHMARK / "_removed.json"
USER_ID = re.compile(r"^[0-9A-Za-z_-]{3,40}$")
REPORT_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}-[^/\\]{1,40}$")
TOKEN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class Missing(Exception):
    """要删的东西不存在。"""


# ---------------------------------------------------------------- 小工具

def _read(f, default):
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default
    except (json.JSONDecodeError, OSError):
        return default


def _write(f, data):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def removed_accounts():
    return set(_read(REMOVED, []))


def hidden_reports():
    return set(_read(HIDDEN, {}).get("reports", []))


def set_removed(uid, on):
    s = removed_accounts()
    s.add(uid) if on else s.discard(uid)
    _write(REMOVED, sorted(s))


def _set_hidden(key, on):
    data = _read(HIDDEN, {})
    s = set(data.get("reports", []))
    s.add(key) if on else s.discard(key)
    data["reports"] = sorted(s)
    _write(HIDDEN, data)


def _purge():
    if not TRASH.exists():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for d in TRASH.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _inbox_files(note_id):
    """一篇采集箱笔记对应的文件：笔记 JSON + _media 里同名的音频。"""
    for f in INBOX.glob("*.json") if INBOX.exists() else []:
        try:
            nid = json.loads(f.read_text(encoding="utf-8")).get("noteId") or f.stem
        except (json.JSONDecodeError, OSError):
            continue
        if nid == note_id or f.stem == note_id:
            media = INBOX / "_media"
            return [f] + ([m for m in media.glob(glob_escape(f.stem) + ".*")] if media.exists() else [])
    return []


def glob_escape(s):
    return re.sub(r"([*?\[\]])", r"[\1]", s)


def _summary():
    """账号增删后重算对标汇总；一个账号都不剩就去掉汇总，页面回到空状态。"""
    import benchmark
    if any(f for f in BENCHMARK.glob("*.json") if not f.name.startswith("_")):
        benchmark.write_summary(0, record=False)
    else:
        (BENCHMARK / "_summary.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- 删除 / 撤销

def delete(kind, ids):
    """删除一批同类条目，返回撤销用的 token。"""
    ids = [str(i) for i in ids if str(i)]
    if not ids:
        raise Missing("没有指定要删除的内容")
    files, extra = [], {}
    if kind == "inbox":
        for i in ids:
            files += _inbox_files(i)
    elif kind == "bench":
        ids = [i for i in ids if USER_ID.match(i)]
        files = [BENCHMARK / f"{i}.json" for i in ids if (BENCHMARK / f"{i}.json").exists()]
    elif kind == "topic-report":
        files = [TOPIC_REPORTS / f"{i}.md" for i in ids
                 if REPORT_FILE.match(i) and (TOPIC_REPORTS / f"{i}.md").exists()]
    elif kind == "report":
        extra["keys"] = ids
    else:
        raise ValueError(f"不支持删除 {kind}")
    if not files and kind != "report":
        raise Missing("内容已不存在，刷新页面后重试")

    _purge()
    token = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    box = TRASH / token
    box.mkdir(parents=True)
    moved = []
    for n, f in enumerate(files):
        name = f"{n:02d}-{f.name}"
        shutil.move(str(f), box / name)
        moved.append([name, str(f)])
    _write(box / "meta.json", {"kind": kind, "ids": ids, "files": moved,
                               "at": datetime.now().isoformat(timespec="seconds"), **extra})

    if kind == "bench":
        for i in ids:
            set_removed(i, True)
        _summary()
    if kind == "report":
        for k in ids:
            _set_hidden(k, True)
    return token


def restore(token):
    """撤销一次删除：文件搬回原处（原处已有新文件就保留新的），隐藏/移除标记去掉。"""
    if not TOKEN.match(token or ""):
        raise Missing("撤销记录无效")
    box = TRASH / token
    meta = _read(box / "meta.json", None)
    if not meta:
        raise Missing("撤销记录已过期")
    for name, orig in meta.get("files", []):
        src, dst = box / name, Path(orig)
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dst)
    if meta["kind"] == "bench":
        for i in meta["ids"]:
            set_removed(i, False)
        _summary()
    if meta["kind"] == "report":
        for k in meta.get("keys", []):
            _set_hidden(k, False)
    shutil.rmtree(box, ignore_errors=True)
