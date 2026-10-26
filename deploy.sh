#!/bin/bash
# ============================================================
# QuantAI 智能量化分析平台 - 一键部署脚本
# 用法：sudo bash deploy.sh
# ============================================================
set -e

PROJECT_DIR="/workspace/quant-platform"
PYTHON="/root/.pyenv/versions/3.14.8/bin/python3"
SERVICE_NAME="quantai"
NGINX_CONF="/etc/nginx/sites-available/quantai"

echo "🚀 [1/6] 检查依赖..."
cd "$PROJECT_DIR"
$PYTHON -m pip install -q -r requirements.txt

echo "🚀 [2/6] 安装 systemd 服务..."
cp deploy/quantai.service /etc/systemd/system/$SERVICE_NAME.service
systemctl daemon-reload
systemctl enable $SERVICE_NAME

echo "🚀 [3/6] 配置 Nginx 反向代理..."
cp deploy/quantai.nginx.conf $NGINX_CONF
ln -sf $NGINX_CONF /etc/nginx/sites-enabled/quantai
rm -f /etc/nginx/sites-enabled/default
nginx -t

echo "🚀 [4/6] 启动服务..."
systemctl restart $SERVICE_NAME
systemctl restart nginx

echo "🚀 [5/6] 健康检查..."
sleep 2
if curl -sf http://127.0.0.1:8080/health > /dev/null; then
    echo "   ✅ 服务运行正常"
else
    echo "   ⚠️  健康检查失败，请查看日志：journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

echo "🚀 [6/6] 部署完成！"
echo ""
echo "   本地访问：  http://127.0.0.1:8080"
echo "   服务日志：  journalctl -u $SERVICE_NAME -f"
echo "   Nginx日志： tail -f /var/log/nginx/quantai-*.log"
echo ""
echo "   如需对外开放 80 端口："
echo "   1. 停止占用 80 端口的进程"
echo "   2. 编辑 $NGINX_CONF，将 listen 8080 改为 listen 80"
echo "   3. nginx -s reload"
