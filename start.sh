#!/bin/bash
# QuantAI 智能量化分析平台启动脚本
cd "$(dirname "$0")"
exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000
