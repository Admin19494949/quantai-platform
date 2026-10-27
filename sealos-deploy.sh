#!/bin/bash
# QuantAI 在 Sealos Devbox 中的一键部署脚本
# 用法（在 Devbox 终端里执行）：
#   cd ~/quantai-platform && bash sealos-deploy.sh
set -e

# pip 会以 --user 方式把可执行文件装到 ~/.local/bin，必须加入 PATH，否则 uvicorn 找不到
export PATH="$HOME/.local/bin:$PATH"

PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
REPO="https://github.com/Admin19494949/quantai-platform.git"
APP_DIR="$HOME/quantai-platform"
PORT=8000

echo "[1/4] 获取代码 ..."
if [ ! -d "$APP_DIR/.git" ]; then
  rm -rf "$APP_DIR"
  git clone --depth 1 "$REPO" "$APP_DIR"
else
  cd "$APP_DIR" && git pull --ff-only || true
fi
cd "$APP_DIR"

echo "[2/4] 安装依赖（清华源）..."
pip install --no-cache-dir -i "$PIP_MIRROR" -r requirements.txt

# 依赖预检：requirements.txt 若漏项，这里会立刻暴露，而不是等到服务启动后才报错
echo "[2.5/4] 校验代码依赖是否齐全 ..."
if ! python3 -c "import server.main" 2>/tmp/quantai_import.err; then
  echo "依赖缺失，缺少以下模块："
  grep -oE "No module named '[^']+'" /tmp/quantai_import.err | sort -u
  echo "请把上面缺失的模块名补进 requirements.txt 后重试。"
  exit 1
fi
echo "依赖校验通过。"

echo "[3/4] 启动服务 ..."
pkill -f "uvicorn server.main:app" 2>/dev/null || true
sleep 1
# 用 python -m uvicorn，避免依赖 PATH 中的可执行文件
nohup python3 -m uvicorn server.main:app --host 0.0.0.0 --port $PORT > /tmp/quantai.log 2>&1 &

echo "[4/4] 健康检查（最多等待 90 秒）..."
CODE=000
for i in $(seq 1 45); do
  sleep 2
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$PORT/api/ai/status" 2>/dev/null || true)
  [ "$CODE" = "200" ] && break
done
echo "本机服务状态: $CODE"
if [ "$CODE" != "200" ]; then
  echo "--- 进程状态 ---"
  pgrep -af "uvicorn server.main:app" || echo "（未找到 uvicorn 进程）"
  echo "--- 最近日志 ---"
  tail -30 /tmp/quantai.log
  exit 1
fi
echo "部署完成。服务已在 0.0.0.0:$PORT 运行。"
echo "下一步：在 Devbox 界面点击「发布 / Release」，启动命令填："
echo "  export PATH=\$HOME/.local/bin:\$PATH && cd \$HOME/quantai-platform && python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000"
echo "端口填 8000"
