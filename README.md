# macOS Mail Agent (Exchange 郵件 AI 幕僚)

輕量、隱私、本地端的 AI 郵件幕僚。透過 macOS 內建「郵件」App 與 AppleScript，
抓取公司 Exchange 帳號收件夾中的新信件，交由 LLM 產出重點摘要、待辦事項與應對方針。

## 篩選規則

只處理**同時**符合以下條件的信件：

1. 指定帳號底下的 `INBOX` (收件夾)
2. **未加旗標** (flagged status = false)
   在手機上已處理完的信，只要加上旗標，Agent 就會略過
3. 收信時間**晚於** `last_sync.json` 記錄的上次執行時間 (首次執行則掃描全部無旗標信件)

## 檔案結構

```text
mail-agent/
├── run_agent.py          # 主程式：AppleScript 讀信 + LLM 分析
├── check_mail.command    # 一鍵啟動檔：雙擊即可在 Terminal 執行
├── requirements.txt      # Python 依賴套件
├── last_sync.json        # 自動生成：上次執行的時間戳記 (已加入 .gitignore)
└── tests/                # 解析邏輯的單元測試
```

## 安裝

```bash
cd ~/src/mail-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

在 `~/.zshrc` 加入 API Key 與要處理的帳號：

```bash
export OPENAI_API_KEY="your-actual-api-key"
export MAIL_AGENT_ACCOUNT="Celxpert,Keelgoal"
```

## 設定

| 環境變數 | 預設值 | 說明 |
|---|---|---|
| `MAIL_AGENT_ACCOUNT` | `Exchange` | 郵件 App 側邊欄顯示的公司帳號名稱，多個以逗號分隔，如 `Celxpert,Keelgoal` |
| `MAIL_AGENT_MAILBOX` | `INBOX` | 收件夾名稱。找不到時會自動嘗試 `Inbox`、`收件匣` 等常見名稱，仍找不到請用 `--list-mailboxes` 查看實際名稱後指定 |
| `OPENAI_MODEL` | `gpt-4o` | 使用的模型，可改 `gpt-4o-mini` |
| `OPENAI_BASE_URL` | (無) | 指向自架 llm-router、Azure OpenAI 或其他 OpenAI 相容介面 |
| `MAIL_AGENT_JSON_MODE` | `auto` | 結構化輸出方式：`schema` 用 OpenAI Structured Outputs；`prompt` 在提示詞中要求純 JSON 再以 pydantic 驗證，相容任何相容介面；`auto` 有設 `OPENAI_BASE_URL` 時用 `prompt`，否則 `schema` |
| `MAIL_AGENT_MAX_BODY_CHARS` | `4000` | 每封信送出的內文字元上限 |
| `MAIL_AGENT_BATCH_SIZE` | `10` | 每次 LLM 呼叫最多處理幾封信 |

### 接到自架 llm-router

llm-router 對外是 OpenAI 相容 API，但不會把 `response_format` 轉送給後端 provider，
因此要走 `prompt` 模式 (`auto` 會自動選到)。在 `~/.zshrc` 設定：

```bash
export OPENAI_BASE_URL="http://127.0.0.1:9001/v1"
export OPENAI_API_KEY="<llm-router 的 LLM_ROUTER_API_KEY>"
export OPENAI_MODEL="<router 裡的虛擬模型名稱>"
```

虛擬模型名稱可用 `curl http://127.0.0.1:9001/v1/models` 查看。郵件內容屬公司資料，
建議在 router 設定一條只含有企業資料條款 provider (Azure、Anthropic、本機 Ollama) 的專用 chain。

不確定帳號名稱時，先列出：

```bash
.venv/bin/python run_agent.py --list-accounts
```

## 執行

雙擊 `check_mail.command`，或在終端機：

```bash
.venv/bin/python run_agent.py
```

其他選項：

| 參數 | 作用 |
|---|---|
| `--dry-run` | 只列出信件主旨、寄件者與時間，不讀內文、不呼叫 LLM、不更新時間戳 (適合首次確認設定，速度快) |
| `--limit N` | 每個帳號最多讀取 N 封，依郵件 App 回傳順序取前 N 封 (通常最新在前)，適合先小量試跑 |
| `--all` | 忽略上次執行時間，掃描所有無旗標信件 |
| `--account NAME` | 臨時指定帳號名稱，多個以逗號分隔 |
| `--list-accounts` | 列出郵件 App 中的帳號 |
| `--list-mailboxes` | 列出 `--account` 指定帳號底下的信箱名稱 (排查找不到收件夾時使用) |

## 注意事項

1. **系統授權**：首次執行時 macOS 會詢問「是否允許 Terminal 控制郵件」，請選擇「允許」。
   若誤按拒絕，到「系統設定 > 隱私權與安全性 > 自動化」重新開啟。
2. **帳號名稱**：`MAIL_AGENT_ACCOUNT` 必須與郵件 App 側邊欄顯示的名稱一致，可用 `--list-accounts` 確認。
   多個帳號請在同一次執行中以逗號指定，不要分次跑：`last_sync.json` 是所有帳號共用的，分次跑會讓後面的帳號漏信。
3. **收件夾名稱**：Exchange 帳號的收件夾在郵件 App 中可能叫 `Inbox` 或「收件匣」而非 `INBOX`，程式會自動嘗試常見名稱；
   若仍報「找不到收件夾」，錯誤訊息會列出該帳號實際的信箱名稱，將其設到 `MAIL_AGENT_MAILBOX` 即可。
4. **資料隱私**：只有「無旗標」且在「收件夾」中的信件文本會送至 LLM API，請確認符合公司 AI 工具使用規範。
5. **時間戳**：同步時間點在**抓信前**記錄，因此執行期間抵達的信件不會被漏掉，下次執行仍會處理。
6. **首次執行很慢**：沒有 `last_sync.json` 時會掃描收件夾中所有未加旗標的信，且每封都要逐一透過 AppleScript 讀取內文。
   建議先手動建立起始時間點，例如只處理某個時間之後的信：

   ```bash
   printf '{\n  "last_time": "2026-10-26 08:00:00"\n}\n' > last_sync.json
   ```

   或先用 `--dry-run --limit 20` 小量確認設定正確。執行時畫面會即時顯示「已讀取 n/N 封」的進度。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
```
