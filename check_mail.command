#!/bin/zsh
# 雙擊即可在 Terminal 執行 macOS Mail Agent
cd "$(dirname "$0")"

# 載入使用者 shell 設定 (取得 OPENAI_API_KEY 等環境變數)
[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc"

# 若專案內有虛擬環境則優先使用
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

"$PY" run_agent.py "$@"
echo ""
read -k 1 "?按任意鍵結束..."
