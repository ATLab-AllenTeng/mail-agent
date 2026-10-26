#!/usr/bin/env bash
# macOS Mail Agent 安裝與操作（macOS；bash 3.2 相容；比照 letta.kb 的 scripts/kb.sh）
#
#   mail-agent install          建 venv、裝依賴、建 .env（金鑰不回顯）、檢查郵件帳號（一次性，可重跑）
#   mail-agent run [參數]       執行一次郵件幕僚；輸出同時存到 ~/.mail-agent/logs/
#   mail-agent dry-run [參數]   只列出會處理的信件，不讀內文、不呼叫 LLM、不更新時間戳
#   mail-agent accounts         列出郵件 App 中的帳號名稱
#   mail-agent mailboxes        列出 .env 所設帳號底下的信箱名稱
#   mail-agent status           環境、設定、LLM 端點、上次同步時間與最近一次執行
#   mail-agent logs [-f]        最近一次執行的輸出
#   mail-agent key              只更換 .env 裡的 API 金鑰（輪換用，不必重跑 install）
#
# 設定一律在專案目錄的 .env（run_agent.py 自行載入）；狀態與 log 在 ~/.mail-agent/。
set -u

# ── 路徑 ─────────────────────────────────────────────────────────────
AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.mail-agent"
LOG_DIR="${STATE_DIR}/logs"
VENV="${AGENT_DIR}/.venv"
PY="${VENV}/bin/python"
ENV_FILE="${AGENT_DIR}/.env"
ENV_EXAMPLE="${AGENT_DIR}/.env.example"
SYNC_FILE="${AGENT_DIR}/last_sync.json"
LATEST_LOG="${LOG_DIR}/latest.log"
BIN_LINK="${HOME}/.local/bin/mail-agent"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { red "錯誤：$*"; exit 1; }
step() { printf '\n\033[36m[%s] %s\033[0m\n' "$1" "$2"; }

# ── .env 讀寫 ────────────────────────────────────────────────────────
# env_get KEY：環境變數優先，其次 .env（去掉成對引號與 export 前綴）
env_get() {
  local key="$1" val
  eval "val=\${${key}:-}"
  if [ -n "${val}" ]; then echo "${val}"; return; fi
  [ -f "${ENV_FILE}" ] || return 0
  sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//p" "${ENV_FILE}" \
    | head -n 1 | sed -E "s/^\"(.*)\"\$/\1/; s/^'(.*)'\$/\1/"
}

# env_set KEY VALUE：已有該行（含被註解掉的）就改，沒有就附加到檔尾
env_set() {
  local key="$1" tmp
  NEWVAL="$2" tmp="$(mktemp "${TMPDIR:-/tmp}/mail-agent.XXXXXX")"
  NEWVAL="$2" awk -v k="${key}" '
    BEGIN { v = ENVIRON["NEWVAL"]; done = 0 }
    !done && $0 ~ ("^[[:space:]]*(#[[:space:]]*)?(export[[:space:]]+)?" k "[[:space:]]*=") {
      print k "=" v; done = 1; next
    }
    { print }
    END { if (!done) print k "=" v }
  ' "${ENV_FILE}" > "${tmp}" && mv "${tmp}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

mask() { # 只顯示末四碼
  local s="$1"
  if [ "${#s}" -le 4 ]; then echo "****"; else printf '****%s\n' "${s: -4}"; fi
}

# ask VAR "提示" "預設"：互動式詢問；非 tty 時直接用預設
ask() {
  local __var="$1" prompt="$2" default="$3" input=""
  if [ -t 0 ]; then
    printf '%s [%s]: ' "${prompt}" "${default}"
    IFS= read -r input
  fi
  eval "${__var}=\"\${input:-\${default}}\""
}

# ask_secret VAR "提示" "既有值"：輸入不回顯；留空＝保留既有值；非 tty 時用環境變數
ask_secret() {
  local __var="$1" prompt="$2" existing="$3" input=""
  if [ -t 0 ]; then
    if [ -n "${existing}" ]; then printf '%s（目前 %s，直接 Enter 保留）: ' "${prompt}" "$(mask "${existing}")"
    else printf '%s: ' "${prompt}"; fi
    IFS= read -r -s input; echo
  fi
  eval "${__var}=\"\${input:-\${existing}}\""
}

# ── 健康檢查 ─────────────────────────────────────────────────────────
alive() { curl -fsS --max-time 2 "$1" >/dev/null 2>&1; }
# 由 MAIL_AGENT_BASE_URL 推 health 網址：llm-router 是 <base 去掉 /v1>/health，其他端點試 /models
endpoint_ok() {
  local base="$1"
  alive "${base%/}/models" || alive "${base%/v1}/health" || alive "${base%/v1/}/health"
}

need_venv() { [ -x "${PY}" ] || die "找不到 ${PY}，請先執行：$0 install"; }
need_env()  { [ -f "${ENV_FILE}" ] || die "找不到 ${ENV_FILE}，請先執行：$0 install"; }

# ── install ──────────────────────────────────────────────────────────
cmd_install() {
  echo "macOS Mail Agent 安裝"
  echo "專案位置：${AGENT_DIR}"

  step 1 "Python"
  command -v python3 >/dev/null 2>&1 || die "找不到 python3，請先安裝（brew install python）"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10 以上，目前是 $(python3 --version 2>&1)"
  grn "  $(python3 --version 2>&1)"

  step 2 "虛擬環境與依賴"
  if [ ! -x "${PY}" ]; then
    python3 -m venv "${VENV}" || die "建立 venv 失敗"
    grn "  已建立 ${VENV}"
  else
    grn "  已有 ${VENV}"
  fi
  "${PY}" -m pip install -q -r "${AGENT_DIR}/requirements.txt" || die "安裝依賴失敗（看上方 pip 訊息）"
  grn "  依賴已就緒：$("${PY}" -c 'import openai, pydantic; print("openai", openai.__version__, "/ pydantic", pydantic.__version__)')"

  step 3 "設定檔 .env"
  if [ ! -f "${ENV_FILE}" ]; then
    cp "${ENV_EXAMPLE}" "${ENV_FILE}" && chmod 600 "${ENV_FILE}"
    grn "  已從 .env.example 建立 ${ENV_FILE}"
  else
    chmod 600 "${ENV_FILE}"
    grn "  已有 ${ENV_FILE}，以下輸入會更新它（直接 Enter 保留目前值）"
  fi
  local acct model base key
  ask acct  "郵件 App 帳號名稱（多個以逗號分隔）" "$(env_get MAIL_AGENT_ACCOUNT)"
  ask base  "LLM 端點 base URL（接 OpenAI 官方請留空）" "$(env_get MAIL_AGENT_BASE_URL)"
  ask model "模型名稱" "$(env_get MAIL_AGENT_MODEL)"
  ask_secret key "API 金鑰（輸入不回顯）" "$(env_get MAIL_AGENT_API_KEY)"
  case "${key}" in ""|"<"*) die "沒有輸入 API 金鑰，.env 其他設定已保存，可重跑 install 或用 '$0 key' 補上" ;; esac
  env_set MAIL_AGENT_ACCOUNT  "${acct}"
  env_set MAIL_AGENT_BASE_URL "${base}"
  env_set MAIL_AGENT_MODEL    "${model}"
  env_set MAIL_AGENT_API_KEY  "${key}"
  grn "  已寫入 ${ENV_FILE}（權限 600，只有本人可讀）"

  step 4 "LLM 端點"
  if [ -n "${base}" ]; then
    if endpoint_ok "${base}"; then grn "  ${base} 有回應"
    else ylw "  ${base} 沒有回應；若是本機 llm-router，請先把它啟動（kb start 或 launchctl）"; fi
  else
    grn "  使用 OpenAI 官方端點"
  fi

  step 5 "郵件帳號"
  echo "  首次會跳出「Terminal 想要控制郵件」的授權視窗，請選擇「允許」。"
  local listing missing="" a
  if listing="$(cd "${AGENT_DIR}" && "${PY}" run_agent.py --list-accounts 2>&1)"; then
    echo "${listing}" | sed -n 's/^   - /    /p'
    local old_ifs="${IFS}"; IFS=','
    for a in ${acct}; do
      a="$(echo "${a}" | sed -E 's/^ +| +$//g')"
      [ -n "${a}" ] && ! echo "${listing}" | grep -qx "   - ${a}" && missing="${missing} ${a}"
    done
    IFS="${old_ifs}"
    if [ -n "${missing}" ]; then ylw "  .env 設定的帳號在郵件 App 找不到：${missing}（請對照上面清單修正 MAIL_AGENT_ACCOUNT）"
    else grn "  帳號確認：${acct}"; fi
  else
    ylw "  無法讀取郵件 App 帳號（可能尚未授權或郵件 App 未啟動），稍後可用 '$0 accounts' 重試"
    echo "${listing}" | tail -n 3 | sed 's/^/    /'
  fi

  step 6 "指令捷徑"
  if [ -d "$(dirname "${BIN_LINK}")" ]; then
    ln -sfn "${AGENT_DIR}/scripts/mail-agent.sh" "${BIN_LINK}"
    grn "  已建立 ${BIN_LINK} → 之後可直接輸入 mail-agent <子命令>"
    case ":${PATH}:" in *":$(dirname "${BIN_LINK}"):"*) ;; *) ylw "  注意：$(dirname "${BIN_LINK}") 不在 PATH 中" ;; esac
  else
    echo "  （沒有 $(dirname "${BIN_LINK}")，略過；可直接用 ${AGENT_DIR}/scripts/mail-agent.sh）"
  fi

  echo
  grn "安裝完成。建議先小量試跑："
  echo "  mail-agent dry-run --limit 20"
  echo "若收件夾很大，先建立起始時間點避免第一次掃描全部舊信："
  echo "  printf '{\"last_time\": \"$(date '+%Y-%m-%d') 08:00:00\"}\\n' > ${SYNC_FILE}"
}

cmd_key() {
  need_env
  local key
  # 非互動 (沒有 tty) 時改用環境變數 MAIL_AGENT_API_KEY 提供的新金鑰
  ask_secret key "新的 API 金鑰（輸入不回顯）" "${MAIL_AGENT_API_KEY:-}"
  [ -n "${key}" ] || die "沒有輸入金鑰"
  env_set MAIL_AGENT_API_KEY "${key}"
  grn "金鑰已更新（$(mask "${key}")）"
}

# ── 執行 ─────────────────────────────────────────────────────────────
run_agent() { # run_agent <log 前綴> [run_agent.py 參數...]
  local prefix="$1"; shift
  need_venv; need_env
  mkdir -p "${LOG_DIR}" || die "無法建立 ${LOG_DIR}"
  local log="${LOG_DIR}/${prefix}-$(date '+%Y%m%d-%H%M%S').log"
  # pipefail：回傳 run_agent.py 的結束碼，而不是 tee 的
  ( cd "${AGENT_DIR}" && set -o pipefail && "${PY}" run_agent.py "$@" 2>&1 | tee "${log}" )
  local rc=$?
  [ "${prefix}" = "run" ] && ln -sfn "${log}" "${LATEST_LOG}"
  # 只保留最近 30 份
  ls -1t "${LOG_DIR}"/"${prefix}"-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null
  return "${rc}"
}

cmd_run()     { run_agent run "$@"; }
cmd_dry_run() { run_agent dry-run --dry-run "$@"; }
cmd_accounts()  { need_venv; ( cd "${AGENT_DIR}" && "${PY}" run_agent.py --list-accounts ); }
cmd_mailboxes() { need_venv; need_env; ( cd "${AGENT_DIR}" && "${PY}" run_agent.py --list-mailboxes "$@" ); }

cmd_logs() {
  local follow=""
  [ "${1:-}" = "-f" ] && follow="-f"
  [ -e "${LATEST_LOG}" ] || die "還沒有任何執行紀錄（${LOG_DIR}）"
  tail ${follow} -n 80 "${LATEST_LOG}"
}

# ── status ───────────────────────────────────────────────────────────
cmd_status() {
  echo "macOS Mail Agent 狀態"
  echo "專案位置：${AGENT_DIR}"
  echo
  if [ -x "${PY}" ]; then grn "● venv        ${VENV}"; else red "○ venv        未建立（執行 install）"; fi
  if [ -f "${ENV_FILE}" ]; then
    local perm; perm="$(stat -f '%Lp' "${ENV_FILE}" 2>/dev/null || echo '?')"
    if [ "${perm}" = "600" ]; then grn "● .env        ${ENV_FILE}（權限 600）"
    else ylw "● .env        ${ENV_FILE}（權限 ${perm}，建議 chmod 600）"; fi
  else
    red "○ .env        未建立（執行 install）"
  fi
  local acct base model key
  acct="$(env_get MAIL_AGENT_ACCOUNT)"; base="$(env_get MAIL_AGENT_BASE_URL)"
  model="$(env_get MAIL_AGENT_MODEL)";  key="$(env_get MAIL_AGENT_API_KEY)"
  echo
  echo "設定（.env，環境變數可覆蓋）："
  echo "  MAIL_AGENT_ACCOUNT  = ${acct:-（未設定，預設 Exchange）}"
  echo "  MAIL_AGENT_MODEL    = ${model:-（未設定，預設 gpt-4o）}"
  echo "  MAIL_AGENT_BASE_URL = ${base:-（未設定，OpenAI 官方端點）}"
  case "${key}" in
    "")   red  "  MAIL_AGENT_API_KEY  = 未設定" ;;
    "<"*) red  "  MAIL_AGENT_API_KEY  = 仍是範本佔位字串，請執行 install 或 key" ;;
    *)    echo "  MAIL_AGENT_API_KEY  = $(mask "${key}")" ;;
  esac
  echo
  if [ -n "${base}" ]; then
    if endpoint_ok "${base}"; then grn "● LLM 端點    ${base} 有回應"; else red "○ LLM 端點    ${base} 沒有回應"; fi
  else
    echo "－ LLM 端點    OpenAI 官方（不做連線檢查）"
  fi
  if [ -f "${SYNC_FILE}" ]; then
    echo "● 上次同步    $(sed -n -E 's/.*"last_time"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' "${SYNC_FILE}")"
  else
    ylw "○ 上次同步    無紀錄（下次執行會掃描所有無旗標信件）"
  fi
  if [ -e "${LATEST_LOG}" ]; then
    echo "● 最近執行    $(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "${LATEST_LOG}" 2>/dev/null)（mail-agent logs 查看）"
  else
    echo "○ 最近執行    尚無紀錄"
  fi
  echo
  echo "LOG_DIR = ${LOG_DIR}"
}

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-help}" in
  install)        cmd_install ;;
  key)            cmd_key ;;
  run)            shift; cmd_run "$@" ;;
  dry-run|dry)    shift; cmd_dry_run "$@" ;;
  accounts)       cmd_accounts ;;
  mailboxes)      shift; cmd_mailboxes "$@" ;;
  status|st)      cmd_status ;;
  logs)           shift; cmd_logs "$@" ;;
  help|-h|--help) usage ;;
  *)              red "未知的子命令：$1"; usage; exit 1 ;;
esac
