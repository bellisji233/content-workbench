#!/bin/bash
# 双击打开本地工作台。关掉这个终端窗口，本地工作台就停止。
# macOS 自带的 python3 是 3.9，工作台需要 3.11+，所以先找新版本。
cd "$(dirname "$0")" || exit 1
for py in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$py" >/dev/null 2>&1; then exec "$py" scripts/serve.py; fi
done
echo "没有找到 Python 3.11 或更高版本。用 Homebrew 安装：brew install python"
