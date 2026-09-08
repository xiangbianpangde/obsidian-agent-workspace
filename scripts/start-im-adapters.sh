#!/usr/bin/env bash
# 个人工作台 IM 实时接入一键启动脚本
# 用法: ./scripts/start-im-adapters.sh
set -euo pipefail

echo "=========================================="
echo " 个人工作台 · 统一消息中心 (IM Hub) 启动器"
echo "=========================================="

WX_CLI="${HOME}/.local/bin/wx-cli"

# ---------------------------------------------------------------------------
# 1. 微信 (wx-cli)
# ---------------------------------------------------------------------------
if [ -x "$WX_CLI" ]; then
  echo ""
  echo "[1/3] 检查微信 wx-cli 服务..."
  if "$WX_CLI" server status 2>/dev/null | grep -q "running"; then
    echo "      ✓ 微信服务已在运行 (http://127.0.0.1:9100)"
  else
    if "$WX_CLI" key list 2>/dev/null | grep -q "raw=yes"; then
      echo "      → 启动微信只读服务..."
      "$WX_CLI" server run --port 9100 >/tmp/wx_cli_server.log 2>&1
      sleep 3
      if "$WX_CLI" server status 2>/dev/null | grep -q "running"; then
        echo "      ✓ 微信服务已启动 (http://127.0.0.1:9100)"
      else
        echo "      ✗ 微信服务启动失败，请查看 /tmp/wx_cli_server.log"
      fi
    else
      echo "      ⚠ 未找到微信解密 Key。请先运行: python backend/scripts/extract_wechat_key.py"
    fi
  fi
else
  echo "[1/3] ⚠ 未安装 wx-cli，跳过微信接入"
fi

# ---------------------------------------------------------------------------
# 2. 企业微信 (快照)
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] 检查企业微信快照..."
SNAP_ROOT="${HOME}/Library/Application Support/wecom-local-vault/snapshots"
if [ -d "$SNAP_ROOT" ] && [ -n "$(ls -A "$SNAP_ROOT" 2>/dev/null)" ]; then
  LATEST=$(ls -1 "$SNAP_ROOT" | sort | tail -n 1)
  echo "      ✓ 发现最新快照: $LATEST"
else
  echo "      ⚠ 未发现企业微信快照。"
  echo "        请先运行: python ~/Projects/vendor/yichen-skills/yichen-wecom-local-vault/scripts/vault_cli.py decrypt \\"
  echo "          --data-dir \"\$HOME/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data\""
fi

# ---------------------------------------------------------------------------
# 3. QQ (Zhin 推送端点)
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] QQ 入站端点: POST http://127.0.0.1:8787/internal/im/ingest/zhin"
echo "      请求头: X-IM-Secret: ${IM_INGEST_SECRET:-workspace_im_secret_token_default}"

# ---------------------------------------------------------------------------
# 4. 工作台
# ---------------------------------------------------------------------------
echo ""
echo "[4/4] 启动个人工作台..."
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/api/im/status 2>/dev/null | grep -q "200"; then
  echo "      ✓ 工作台已在运行 (http://127.0.0.1:8787)"
else
  cd "$(dirname "$0")/.."
  nohup .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8787 >/tmp/workspace_serve.log 2>&1 &
  sleep 5
  echo "      ✓ 工作台已启动 (http://127.0.0.1:8787)"
fi

echo ""
echo "=========================================="
echo " 全部就绪！打开 http://127.0.0.1:8787"
echo " 点击顶部【统一消息中心 (IM Hub)】即可查看全部个人消息"
echo "=========================================="
