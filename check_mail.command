#!/bin/zsh
# 雙擊即可在 Terminal 執行 macOS Mail Agent
# 設定一律來自專案目錄的 .env (由 run_agent.py 自行載入)，刻意不載入 ~/.zshrc，
# 避免全域的 OPENAI_* 等變數干擾。
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
  echo "❌ 找不到 .env，請先執行：cp .env.example .env && chmod 600 .env，再填入設定。"
  read -k 1 "?按任意鍵結束..."
  exit 1
fi

# 若專案內有虛擬環境則優先使用
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

"$PY" run_agent.py "$@"
echo ""
read -k 1 "?按任意鍵結束..."
