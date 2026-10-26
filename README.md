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

在 `~/.zshrc` 加入 API Key：

```bash
export OPENAI_API_KEY="your-actual-api-key"
```

## 設定

| 環境變數 | 預設值 | 說明 |
|---|---|---|
| `MAIL_AGENT_ACCOUNT` | `Exchange` | 郵件 App 側邊欄顯示的公司帳號名稱 |
| `MAIL_AGENT_MAILBOX` | `INBOX` | 要讀取的信箱資料夾 |
| `OPENAI_MODEL` | `gpt-4o` | 使用的模型，可改 `gpt-4o-mini` |
| `OPENAI_BASE_URL` | (無) | 指向 Azure OpenAI 或其他相容介面 |
| `MAIL_AGENT_MAX_BODY_CHARS` | `4000` | 每封信送出的內文字元上限 |
| `MAIL_AGENT_BATCH_SIZE` | `10` | 每次 LLM 呼叫最多處理幾封信 |

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
| `--dry-run` | 只抓信並列出，不呼叫 LLM、不更新時間戳 (適合首次確認設定) |
| `--all` | 忽略上次執行時間，掃描所有無旗標信件 |
| `--account NAME` | 臨時指定帳號名稱 |
| `--list-accounts` | 列出郵件 App 中的帳號 |

## 注意事項

1. **系統授權**：首次執行時 macOS 會詢問「是否允許 Terminal 控制郵件」，請選擇「允許」。
   若誤按拒絕，到「系統設定 > 隱私權與安全性 > 自動化」重新開啟。
2. **帳號名稱**：`MAIL_AGENT_ACCOUNT` 必須與郵件 App 側邊欄顯示的名稱一致，可用 `--list-accounts` 確認。
3. **資料隱私**：只有「無旗標」且在「收件夾」中的信件文本會送至 LLM API，請確認符合公司 AI 工具使用規範。
4. **時間戳**：同步時間點在**抓信前**記錄，因此執行期間抵達的信件不會被漏掉，下次執行仍會處理。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
```
