#!/usr/bin/env python3
"""本地工作台：每次打开都现场读最新数据，分析调用本机的 Claude Code。

用法（在工作台目录下运行）:
  python3 scripts/serve.py              启动并在浏览器打开
  python3 scripts/serve.py --no-open    只启动
  python3 scripts/serve.py --port 8765
  python3 scripts/serve.py --demo       打开示例工作台（虚构数据，端口 8770）

页面每 5 秒检查一次数据，有变化就自动刷新：插件采集、转写完成、works/ 新增一期、
Obsidian 改稿、放进新的平台导出，都会自己出现。放进新的平台导出后，选题报告自动重算。

线上页面的三项平台能力，这里各给一份本地实现：
  存储   状态与分析存 local/data/state/{collection}/{id}.json，结构和线上数据库一致
  分析   调用本机 claude -p，用当前登录的 Claude 账号额度
  保存   「保存分析」直接并入资产总库

只监听 127.0.0.1；接口要带启动时生成的口令，别的网页拿不到，也就调不动本机的 claude。
"""

import codecs
import hashlib
import http.server
import os
import json
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

# --demo：用 examples/demo/ 里的虚构数据，必须在导入 paths 之前切换
if "--demo" in sys.argv:
    _demo = Path(__file__).resolve().parents[1] / "examples" / "demo" / "local"
    if not _demo.exists():
        subprocess.run([sys.executable, str(_demo.parents[1] / "make_demo.py")], check=True)
    os.environ["WORKBENCH_LOCAL"] = str(_demo)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402
import build_workbench as bw  # noqa: E402
import classify  # noqa: E402
import merge_assets  # noqa: E402
import topic_report  # noqa: E402
from paths import (AGENT_CWD, EXPORTS, INBOX, LOCAL, STATE,  # noqa: E402
                   TOPIC_REPORTS)



def load_token():
    """口令只生成一次：服务重启后，已经打开的页面还能接着用。"""
    f = STATE / ".token"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    STATE.mkdir(parents=True, exist_ok=True)
    f.write_text(secrets.token_urlsafe(18))
    f.chmod(0o600)
    return f.read_text().strip()


TOKEN = load_token()
COLLECTIONS = {"status", "analyses"}
DOC_ID = re.compile(r"^[\w.-]{1,160}$")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 存储

def doc_path(coll, doc_id):
    if coll not in COLLECTIONS or not DOC_ID.match(doc_id) or ".." in doc_id:
        return None
    return STATE / coll / f"{doc_id}.json"


def list_docs(coll):
    d = STATE / coll
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not data.get("_deleted"):
            out.append({"id": f.stem, "data": data})
    return out


def write_doc(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 数据快照

_lock = threading.Lock()
_transcribe_tried = {}          # 待转写的一组笔记 → 上次尝试的时间


def transcribe_once():
    """有新的待转写视频就在后台起转写。

    同一组待转写的笔记 6 小时内只试一次：下载失败（直链过期）不会留标记，
    不限制的话每次检查都会重新拉起转写。
    """
    lock = INBOX / "_media" / ".running"
    if lock.exists():
        return [l for l in lock.read_text(encoding="utf-8").splitlines() if l]
    pending = []
    for f in sorted(INBOX.glob("*.json")) if INBOX.exists() else []:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if p.get("type") == "视频" and not p.get("transcriptText") \
                and (p.get("video") or {}).get("url"):
            pending.append(f.name)
    key = tuple(pending)
    if not pending or time.time() - _transcribe_tried.get(key, 0) < 6 * 3600:
        return []
    _transcribe_tried[key] = time.time()
    subprocess.Popen([sys.executable, str(Path(__file__).parent / "transcribe_inbox.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return pending


bw.auto_transcribe = transcribe_once


# 选题方向的后台任务：归类新作品或重新划分，要调 agent，几十秒到几分钟，不能挡着页面
_job = {"status": "idle", "error": "", "kind": "", "tried": None}
_job_lock = threading.Lock()


def start_classify(kind="ensure", hint=""):
    """后台归类（ensure）或重新划分（partition），完成后重算今天的报告。同一时间只跑一个。"""
    with _job_lock:
        if _job["status"] == "running":
            return False
        _job.update(status="running", error="", kind=kind)

    def work():
        try:
            items = classify.all_items()
            if kind == "partition":
                classify.partition(items, hint, log=lambda *_: None)
            else:
                classify.ensure(items, log=lambda *_: None)
            regenerate_topic_report()
            _mark_exports_seen()
            _job.update(status="idle")
        except Exception as e:                  # 失败原因显示在页面上，可以手动重试
            _job.update(status="error", error=str(e)[:200])

    threading.Thread(target=work, daemon=True).start()
    return True


def _exports():
    return [p for p in EXPORTS.rglob("*.xlsx") if not p.name.startswith("~$")] \
        if EXPORTS.exists() else []


def _mark_exports_seen():
    xs = _exports()
    if xs:
        TOPIC_REPORTS.mkdir(parents=True, exist_ok=True)
        (TOPIC_REPORTS / ".last-export").write_text(str(max(p.stat().st_mtime for p in xs)))


def refresh_topic_report():
    """放进了新的平台导出，就重算今天的报告。

    「新」按上次自动重算时见过的最新导出时间判断，不按报告文件的时间：
    页面上删掉一份报告后，不会因为报告变旧了又自动生成回来。
    有作品还没归入选题方向时，先在后台归类，归完再出报告；
    同一批作品归类失败不自动重试，页面上可以手动再试。
    """
    xs = _exports()
    if not xs:
        return
    if _job["status"] == "running":
        return
    items = classify.all_items()
    if classify.needs_work(items) and agent.available():
        batch = tuple(sorted(i["key"] for i in classify.pending(items)))
        if _job["tried"] != batch:
            _job["tried"] = batch
            start_classify()
            return
    newest_export = max(p.stat().st_mtime for p in xs)
    seen_f = TOPIC_REPORTS / ".last-export"
    try:
        seen = float(seen_f.read_text())
    except (OSError, ValueError):
        reports = list(TOPIC_REPORTS.glob("*-自有内容.md")) if TOPIC_REPORTS.exists() else []
        seen = max((p.stat().st_mtime for p in reports), default=0)
    if seen < newest_export:
        TOPIC_REPORTS.mkdir(parents=True, exist_ok=True)
        out = TOPIC_REPORTS / f"{topic_report.TODAY}-自有内容.md"
        out.write_text(topic_report.build_report(), encoding="utf-8")
    if not seen_f.exists() or seen < newest_export:
        TOPIC_REPORTS.mkdir(parents=True, exist_ok=True)
        seen_f.write_text(str(max(seen, newest_export)))


def regenerate_topic_report():
    """手动重新生成今天的自有内容报告。和最近一份只差日期时不另存，返回 (是否有变化, 日期)。"""
    with _lock:
        topic_report.TODAY = date.today().isoformat()
        body = topic_report.build_report()
        reports = sorted(TOPIC_REPORTS.glob("*-自有内容.md")) if TOPIC_REPORTS.exists() else []
        undated = lambda t: re.sub(r"\d{4}-\d{2}-\d{2}", "", t)
        if reports and undated(reports[-1].read_text(encoding="utf-8")) == undated(body):
            return False, reports[-1].name[:10]
        TOPIC_REPORTS.mkdir(parents=True, exist_ok=True)
        (TOPIC_REPORTS / f"{topic_report.TODAY}-自有内容.md").write_text(body, encoding="utf-8")
        return True, topic_report.TODAY


def open_folder(folder):
    """在系统文件管理器里打开文件夹。"""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    elif os.name == "nt":
        os.startfile(str(folder))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def snapshot():
    """收件、转写、重算报告，再生成一份完整数据。版本号是数据内容的指纹。"""
    with _lock:
        bw.TODAY = date.today()
        topic_report.TODAY = date.today().isoformat()
        try:
            refresh_topic_report()
        except Exception as e:              # 报告失败不该挡住页面
            print(f"选题报告重算失败：{e}", file=sys.stderr)
        payload = bw.to_payload(*bw.build())
    payload["topicsJob"] = {k: _job[k] for k in ("status", "error", "kind")}
    payload["topicsAgent"] = agent.available()
    stable = {k: v for k, v in payload.items() if k != "generated"}
    payload["version"] = hashlib.sha1(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    payload["localApi"] = {"token": TOKEN}
    return payload


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "workbench"

    def log_message(self, fmt, *args):
        pass

    # ---- 通用 ----
    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self):
        return self._host_ok() and self.headers.get("X-Workbench-Token") == TOKEN

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8") if n else ""

    def _doc_target(self):
        parts = unquote(urlparse(self.path).path).split("/")   # /api/db/{coll}/{id}
        if len(parts) != 5 or parts[1:3] != ["api", "db"]:
            return None
        return doc_path(parts[3], parts[4])

    # ---- 路由 ----
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._send(200, bw.render(snapshot()), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        if path == "/api/version":
            return self._json(200, {"version": snapshot()["version"]})
        if path == "/api/retro/prompt":                 # 复盘爆款库：页面拿提示词交给本机 agent 分析
            import asset_retro
            with _lock:
                prompt, n = asset_retro.build_prompt()
            return self._json(200, {"prompt": prompt, "pending": n})
        m = re.fullmatch(r"/api/db/(\w+)", path)
        if m and m.group(1) in COLLECTIONS:
            return self._json(200, {"docs": list_docs(m.group(1))})
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        target = self._doc_target()
        if not target:
            return self._json(400, {"error": "bad path"})
        try:
            data = json.loads(self._body() or "{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        write_doc(target, {**data, "_savedAt": now_iso()})
        self._json(200, {"ok": True})

    def do_DELETE(self):
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        target = self._doc_target()
        if not target:
            return self._json(400, {"error": "bad path"})
        # 留一条删除记录，同步到线上时才知道这一条是删掉了，不是没同步过
        write_doc(target, {"_deleted": True, "_savedAt": now_iso()})
        self._json(200, {"ok": True})

    def do_POST(self):
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            body = json.loads(self._body() or "{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        if path == "/api/sample":
            return self._sample(str(body.get("prompt") or ""))
        if path == "/api/topic-report":
            try:
                changed, day = regenerate_topic_report()
                return self._json(200, {"changed": changed, "date": day})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if path == "/api/retro/apply":                  # 复盘爆款库：用户确认后写入
            import asset_retro
            with _lock:
                done, errors = asset_retro.apply_plan(body)
            return self._json(200, {"done": done, "errors": errors})
        if path == "/api/topics":                       # 选题方向：改单条、重新划分、归类新作品、撤销
            act = str(body.get("action") or "")
            try:
                if act == "assign":
                    classify.set_category(str(body.get("title") or ""), str(body.get("cat") or ""))
                elif act == "undo":
                    classify.undo()
                elif act in ("partition", "ensure"):
                    if not agent.available():
                        return self._json(500, {"error": agent.missing_message(agent.analysis_cmd(False)[0])})
                    return self._json(200, {"started": start_classify(act, str(body.get("hint") or ""))})
                else:
                    return self._json(400, {"error": "bad action"})
                regenerate_topic_report()
                return self._json(200, {"ok": True})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if path in ("/api/delete", "/api/restore"):     # 删除进回收站，可撤销
            import trash
            try:
                with _lock:
                    if path == "/api/restore":
                        trash.restore(str(body.get("token") or ""))
                        return self._json(200, {"ok": True})
                    ids = body.get("ids") or [body.get("id")]
                    return self._json(200, {"token": trash.delete(str(body.get("kind") or ""), ids)})
            except (trash.Missing, ValueError) as e:
                return self._json(404, {"error": str(e)})
        if path == "/api/open":
            folder = bw.source_dir(str(body.get("key") or ""))
            if not folder:
                return self._json(404, {"error": "未设置"})
            if str(body.get("key")).startswith("export:"):
                folder.mkdir(parents=True, exist_ok=True)
            if not folder.exists():
                return self._json(404, {"error": "文件夹不存在"})
            open_folder(folder)
            return self._json(200, {"ok": True})
        if path == "/api/export":
            try:
                return self._json(200, {"added": merge_assets.merge_record(body)})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def _sample(self, prompt):
        """流式返回分析文本：每行一个 JSON，{"t": 新增文字} / {"error": 原因}。"""
        if not prompt.strip():
            return self._json(400, {"error": "empty prompt"})
        AGENT_CWD.mkdir(parents=True, exist_ok=True)
        try:
            cmd, fmt = agent.analysis_cmd()
            proc = subprocess.Popen(cmd, cwd=AGENT_CWD, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return self._json(500, {"error": agent.missing_message(cmd)})

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        finished = threading.Event()

        def watch_disconnect():
            """页面点「中止」会断开连接：连接一断就结束分析进程，不等它下一次输出。"""
            while not finished.is_set() and proc.poll() is None:
                try:
                    ready, _, _ = select.select([self.connection], [], [], 1)
                    if ready and self.connection.recv(1, socket.MSG_PEEK) == b"":
                        proc.kill()
                        return
                except (OSError, ValueError):     # 请求已结束、连接已关
                    return

        threading.Thread(target=watch_disconnect, daemon=True).start()
        got_result = False
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            if fmt == "text":
                # 纯文本命令：有多少转多少，按 UTF-8 增量解码，免得切坏半个汉字
                dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while chunk := os.read(proc.stdout.fileno(), 4096):
                    text = dec.decode(chunk)
                    if text:
                        emit({"t": text})
                proc.wait()
                if proc.returncode == -9:
                    return
                if proc.returncode != 0:
                    err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
                    emit({"error": err[-1] if err else f"分析命令退出码 {proc.returncode}"})
                return
            for line in proc.stdout:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = e.get("event") or {}
                delta = ev.get("delta") or {}
                if e.get("type") == "stream_event" and delta.get("type") == "text_delta":
                    emit({"t": delta.get("text", "")})
                elif e.get("type") == "result":
                    got_result = True
                    if e.get("is_error"):
                        emit({"error": str(e.get("result") or e.get("subtype") or "分析失败")})
            proc.wait()
            if proc.returncode == -9:
                return                       # 页面已断开，不用再回写
            if not got_result:
                err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
                emit({"error": err[-1] if err else f"claude 退出码 {proc.returncode}"})
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()                      # 页面点了「中止」
        finally:
            finished.set()
            if proc.poll() is None:
                proc.kill()


def is_workbench(url):
    """端口上跑的是不是工作台：看响应头里的服务名。"""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "favicon.ico", timeout=2) as r:
            return r.headers.get("Server", "").startswith("workbench")
    except Exception:
        return False


def first_run():
    """没有配置时从 local.example/ 建好 local/，然后用新配置重启自己。"""
    if "--demo" in sys.argv or (LOCAL / "config.toml").exists():
        return
    import onboard
    onboard.init()
    print("第一次使用：已从模板建好配置。对 agent 说「建工作台」逐步配置；"
          f"也可以直接编辑 {LOCAL / 'config.toml'}，改完刷新页面。", flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    first_run()
    default = 8770 if "--demo" in sys.argv else 8765
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else default
    url = f"http://127.0.0.1:{port}/"
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if not is_workbench(url):
            print(f"端口 {port} 被其他程序占用，换一个端口：--port 端口号")
            sys.exit(1)
        # 已经在跑了就直接打开，不再起第二个
        print(f"本地工作台已在运行：{url}")
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        return
    print(f"✓ 本地工作台 {url}")
    print("  Ctrl+C 停止")
    if "--no-open" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
