#!/usr/bin/env python3
"""给采集箱里的视频笔记补口播文案。

用法:  python3 scripts/transcribe_inbox.py              转写所有还没转写的
       python3 scripts/transcribe_inbox.py --model base 换模型（tiny/base/small/medium）
       python3 scripts/transcribe_inbox.py --force      重转已有的

为什么需要这一步：视频笔记的 body 只是简介，不是口播。不转写就分析，
拆出来的"结构骨架""话术"全是猜的。

链路：插件抓到的视频直链 → 下载 → ffmpeg 提音轨 → faster-whisper → 繁转简 → 写回 JSON。
不需要 yt-dlp —— 直链在采集时就从 __INITIAL_STATE__ 里拿到了。
注意：直链带签名，约一周过期。过期的会提示重新采集那篇。
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import INBOX  # noqa: E402

MEDIA = INBOX / "_media"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return True
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 18):
                f.write(chunk)
        return dest.stat().st_size > 0
    except urllib.error.HTTPError as e:
        print(f"    下载失败 HTTP {e.code}" + ("（直链已过期，重新采集这篇即可）" if e.code in (403, 404) else ""))
    except Exception as e:
        print(f"    下载失败：{e}")
    dest.unlink(missing_ok=True)
    return False


def to_wav(src, dest):
    """16k 单声道 —— whisper 要的就是这个，转完体积也小得多。"""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dest)],
        capture_output=True)
    if r.returncode != 0:
        print(f"    提音频失败：{r.stderr.decode()[-200:]}")
        return False
    return True


def transcribe(wav, model):
    from faster_whisper import WhisperModel
    import opencc
    cc = opencc.OpenCC("t2s")
    segments, info = model.transcribe(str(wav), language="zh", word_timestamps=False)
    lines, plain = [], []
    for s in segments:
        text = cc.convert(s.text).strip()
        if not text:
            continue
        # 保留时间戳：节奏和情绪曲线分析要用
        lines.append({"start": round(s.start, 1), "end": round(s.end, 1), "text": text})
        plain.append(text)
    return lines, "".join(plain), info.duration


def main():
    force = "--force" in sys.argv
    size = arg("--model", "small")

    if not INBOX.exists():
        print("采集箱还是空的。先用插件采几篇。")
        return
    todo = []
    for f in sorted(INBOX.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("type") != "视频":
            continue
        if d.get("transcript") and not force:
            continue
        if not (d.get("video") or {}).get("url"):
            print(f"·  {d.get('title','?')[:30]} —— 没有视频直链，跳过")
            continue
        todo.append((f, d))

    if not todo:
        print("没有需要转写的视频笔记。")
        return

    MEDIA.mkdir(parents=True, exist_ok=True)
    lock = MEDIA / ".running"
    lock.write_text("\n".join(d.get("title", "") for _, d in todo), encoding="utf-8")
    print(f"待转写 {len(todo)} 篇，模型 {size}（首次会下载模型，之后有缓存）\n")

    from faster_whisper import WhisperModel
    model = WhisperModel(size, device="cpu", compute_type="int8")

    ok = 0
    for f, d in todo:
        title = d.get("title", f.stem)[:34]
        stem = f.stem
        print(f"▸ {title}")
        mp4, wav = MEDIA / f"{stem}.mp4", MEDIA / f"{stem}.wav"

        print("    下载视频…", end="", flush=True)
        if not download(d["video"]["url"], mp4):
            continue
        print(f" {mp4.stat().st_size/1024/1024:.1f} MB")

        print("    提取音轨…", end="", flush=True)
        if not to_wav(mp4, wav):
            continue
        print(" ok")

        dur = d["video"].get("duration") or 0
        print(f"    转写中（音频 {dur}s，CPU 上大约要 {max(1, dur//60)}–{max(2, dur//25)} 分钟）…",
              end="", flush=True)
        lines, plain, _ = transcribe(wav, model)
        print(f" {len(lines)} 句 / {len(plain)} 字")

        d["transcript"] = lines
        d["transcriptText"] = plain
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        mp4.unlink(missing_ok=True)          # 音频留着，视频删掉省空间
        ok += 1

    lock.unlink(missing_ok=True)
    print(f"\n✓ 转写完成 {ok}/{len(todo)} 篇")
    if ok:
        print("  重跑 build_workbench.py，采集箱里这些会从「待转写」变成「可分析」，")
        print("  分析时 Claude 拿到的就是完整口播，不再是简介。")


if __name__ == "__main__":
    main()
