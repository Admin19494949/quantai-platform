#!/bin/bash
# Sealos DevBox 发布启动脚本
# 作用：DevBox 发布成镜像后，容器启动时按本脚本拉起 QuantAI 服务
set -e

# 1. 定位应用目录（兼容 DevBox 原始路径 / 镜像内路径）
APP_DIR=""
for d in "$HOME/quantai-platform" "/home/devbox/quantai-platform" "/app" "/root/quantai-platform" "/workspace/quant-platform"; do
  if [ -f "$d/server/main.py" ]; then
    APP_DIR="$d"
    break
  fi
done

if [ -z "$APP_DIR" ]; then
  echo "[entrypoint] 未找到 quantai-platform 目录，请检查镜像内容"
  exit 1
fi

# 2. pip --user 安装的依赖在 ~/.local，需要加入 PATH
export PATH="$HOME/.local/bin:/home/devbox/.local/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
export PIP_NO_CACHE_DIR=1

cd "$APP_DIR"
echo "[entrypoint] 工作目录: $APP_DIR"

# 3. 依赖兜底：镜像内若缺失依赖则补装
if ! python3 -c "import fastapi, uvicorn, scipy" >/dev/null 2>&1; then
  echo "[entrypoint] 检测到依赖缺失，开始安装 ..."
  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
fi

# 4. 启动服务
PORT="${PORT:-8000}"
echo "[entrypoint] 启动 QuantAI: 0.0.0.0:$PORT"
exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port "$PORT"
