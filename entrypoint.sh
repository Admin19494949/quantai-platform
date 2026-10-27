#!/bin/bash
# Sealos DevBox 应用启动脚本
# 说明：entrypoint.sh 只负责启动应用，不做构建（构建/装依赖在开发环境完成）
set -e

export PATH="$HOME/.local/bin:/home/devbox/.local/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1

# 定位项目目录（兼容 DevBox 原始路径与镜像内路径）
APP_DIR="$HOME/quantai-platform"
for d in "$HOME/quantai-platform" "/home/devbox/quantai-platform" "/app" "/workspace/quant-platform"; do
  if [ -f "$d/server/main.py" ]; then
    APP_DIR="$d"
    break
  fi
done

cd "$APP_DIR"
echo "[entrypoint] 工作目录: $APP_DIR"

# 端口：优先用环境变量 PORT，默认 8000
PORT="${PORT:-8000}"
echo "[entrypoint] 启动 QuantAI: 0.0.0.0:$PORT"

exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port "$PORT"
