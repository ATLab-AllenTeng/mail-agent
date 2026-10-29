# macOS Mail Agent (Exchange 郵件 AI 幕僚)

輕量、隱私、本地端的 AI 郵件幕僚。透過 macOS 內建「郵件」App 與 AppleScript，
抓取公司 Exchange 帳號收件夾中的新信件，交由 LLM 產出重點摘要、待辦事項與應對方針。

## 篩選規則

只處理**同時**符合以下條件的信件：

1. 指定帳號底下的 `INBOX` (收件夾)
2. **未加旗標** (flagged status = false)
   在手機上已處理完的信，只要加上旗標，Agent 就會略過
3. 收信時間**晚於** `last_sync.json` 記錄的上次執行時間 (首次執行則掃描全部無旗標信件)

符合條件的信件，除了內文，**附件也會一併納入分析**：由 docconvert 在本機抽成純文字接在內文後，
圖片附件只列檔名 (詳見「附件」一節；可用 `MAIL_AGENT_SKIP_ATTACHMENTS=1` 關掉)。

## 檔案結構

```text
mail-agent/
├── run_agent.py          # 主程式：AppleScript 讀信 + LLM 分析
├── check_mail.command    # 一鍵啟動檔：雙擊即可在 Terminal 執行
├── scripts/mail-agent.sh # 安裝與操作腳本：install / run / dry-run / accounts / status / logs
├── requirements.txt      # Python 依賴套件
├── .env.example          # 設定範本：複製為 .env 後填入 (已加入 .gitignore)
├── last_sync.json        # 自動生成：上次執行的時間戳記 (已加入 .gitignore)
└── tests/                # 解析邏輯的單元測試
```

## 安裝

一鍵安裝 (比照 letta.kb 的 `kb.sh`)：

```bash
cd ~/src/mail-agent && scripts/mail-agent.sh install
```

它會依序：檢查 Python 3.10+、建立 `.venv` 並安裝依賴、從 `.env.example` 建立 `.env` 並詢問
帳號、端點、模型與金鑰 (金鑰輸入不回顯，檔案權限 600)、檢查 LLM 端點是否有回應、
讀取郵件 App 的帳號清單核對 `.env` 的設定，最後在 `~/.local/bin/mail-agent` 建立捷徑。
可以重複執行，既有設定直接 Enter 保留。

安裝後的日常指令：

| 指令 | 作用 |
|---|---|
| `mail-agent run [參數]` | 執行一次郵件幕僚，輸出同時存到 `~/.mail-agent/logs/` |
| `mail-agent dry-run [參數]` | 只列出會處理的信件，不讀內文、不呼叫 LLM、不更新時間戳 |
| `mail-agent accounts` | 列出郵件 App 的帳號名稱 |
| `mail-agent mailboxes` | 列出 `.env` 所設帳號底下的信箱名稱 |
| `mail-agent status` | venv、.env、設定摘要 (金鑰只顯示末四碼)、LLM 端點、上次同步時間、最近一次執行 |
| `mail-agent logs [-f]` | 最近一次執行的輸出 |
| `mail-agent key` | 只更換 API 金鑰，不必重跑 install |

手動安裝也可以：

```bash
cd ~/src/mail-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

然後編輯 `.env` 填入帳號名稱、LLM 端點與金鑰。

要讓附件內容進入分析，另外安裝 [docconvert](https://github.com/ATLab-AllenTeng/docconvert) (選配，沒裝只會列附件檔名)：

```bash
git clone https://github.com/ATLab-AllenTeng/docconvert.git ~/src/docconvert && ~/src/docconvert/scripts/docconvert.sh install --skip-marker
```

`--skip-marker` 跳過視覺模型用的 marker venv (數 GB)；mail-agent 不會用到它。

## 設定

設定一律放在專案目錄的 `.env`，由 `run_agent.py` 自行載入，不需要也不建議寫進 `~/.zshrc`：
所有變數都用 `MAIL_AGENT_` 前綴，程式也會把金鑰與端點明確傳給 SDK，
所以 `~/.zshrc` 裡給其他應用用的 `OPENAI_*` 不會干擾這個專案，反之亦然。
已存在的環境變數優先於 `.env`，臨時覆蓋可用 `MAIL_AGENT_MODEL=xxx .venv/bin/python run_agent.py`。

| 變數 | 預設值 | 說明 |
|---|---|---|
| `MAIL_AGENT_ACCOUNT` | `Exchange` | 郵件 App 側邊欄顯示的公司帳號名稱，多個以逗號分隔，如 `Celxpert,Keelgoal` |
| `MAIL_AGENT_MAILBOX` | `INBOX` | 收件夾名稱。找不到時會自動嘗試 `Inbox`、`收件匣` 等常見名稱，仍找不到請用 `--list-mailboxes` 查看實際名稱後指定 |
| `MAIL_AGENT_API_KEY` | (無，必填) | LLM 端點的金鑰：接 llm-router 填它的 `LLM_ROUTER_API_KEY`，接 OpenAI 填 OpenAI 金鑰 |
| `MAIL_AGENT_BASE_URL` | (無) | 指向自架 llm-router、Azure OpenAI 或其他 OpenAI 相容介面；留空則用 OpenAI 官方端點 |
| `MAIL_AGENT_MODEL` | `gpt-4o` | 模型名稱；接 llm-router 填虛擬模型名稱如 `Mail-Briefing` |
| `MAIL_AGENT_JSON_MODE` | `auto` | 結構化輸出方式：`schema` 用 OpenAI Structured Outputs；`prompt` 在提示詞中要求純 JSON 再以 pydantic 驗證，相容任何相容介面；`auto` 有設 `MAIL_AGENT_BASE_URL` 時用 `prompt`，否則 `schema` |
| `MAIL_AGENT_MAX_BODY_CHARS` | `4000` | 每封信送出的內文字元上限 |
| `MAIL_AGENT_BATCH_SIZE` | `10` | 每次 LLM 呼叫最多處理幾封信 |
| `MAIL_AGENT_SKIP_ATTACHMENTS` | `0` | 設 `1` 完全不處理附件 (不列、不存、不轉換)，只送信件內文 |
| `MAIL_AGENT_DOCCONVERT_BIN` | `~/.local/bin/docconvert` | docconvert 可執行檔；找不到時退回 `node ~/src/docconvert/bin/docconvert.mjs`，再找不到就只列附件檔名並提示安裝 |
| `MAIL_AGENT_MAX_ATTACHMENT_CHARS` | `3000` | 每個附件送出的純文字字元上限，超過會截斷並註明原長 |
| `MAIL_AGENT_DOCCONVERT_TIMEOUT` | `600` | 單封信附件轉換的逾時秒數，逾時該封附件標為擷取失敗，不影響其他信 |
| `MAIL_AGENT_ATTACHMENT_DIR` | `~/.mail-agent/attachments` | 附件暫存根目錄，每次執行建立 `<run-id>/<帳號>/<信件序號>/`，處理完即刪除 |

未設定 `MAIL_AGENT_API_KEY` / `MAIL_AGENT_BASE_URL` / `MAIL_AGENT_MODEL` 時才會退回讀取
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，純粹為了向下相容。

### 接到自架 llm-router

llm-router 對外是 OpenAI 相容 API，但不會把 `response_format` 轉送給後端 provider，
因此要走 `prompt` 模式 (`auto` 會自動選到)。`.env` 這樣填：

```bash
MAIL_AGENT_BASE_URL=http://127.0.0.1:9001/v1
MAIL_AGENT_API_KEY=<llm-router 的 LLM_ROUTER_API_KEY>
MAIL_AGENT_MODEL=Mail-Briefing
```

`Mail-Briefing` 是 router 裡專為郵件摘要建立的 chain，只含有企業資料條款或本機的 provider
(Apple Intelligence、Azure、Anthropic、本機 Ollama)。虛擬模型清單可用
`curl http://127.0.0.1:9001/v1/models` 查看。

不確定帳號名稱時，先列出：

```bash
.venv/bin/python run_agent.py --list-accounts
```

## 執行

雙擊 `check_mail.command`，或在終端機：

```bash
mail-agent run
```

不經腳本直接執行也可以：

```bash
.venv/bin/python run_agent.py
```

其他選項：

| 參數 | 作用 |
|---|---|
| `--dry-run` | 只列出信件主旨、寄件者、時間與附件檔名/大小，不讀內文、不存附件、不轉換、不呼叫 LLM、不更新時間戳 (適合首次確認設定，速度快) |
| `--limit N` | 每個帳號最多讀取 N 封，依郵件 App 回傳順序取前 N 封 (通常最新在前)，適合先小量試跑 |
| `--all` | 忽略上次執行時間，掃描所有無旗標信件 |
| `--account NAME` | 臨時指定帳號名稱，多個以逗號分隔 |
| `--list-accounts` | 列出郵件 App 中的帳號 |
| `--list-mailboxes` | 列出 `--account` 指定帳號底下的信箱名稱 (排查找不到收件夾時使用) |

## 附件

正常執行時，每封信的附件會經過以下流程：

1. AppleScript 把 `mail attachments` 中**非圖片**的附件存到 `~/.mail-agent/attachments/<run-id>/<帳號>/<信件序號>/<附件序號>-<檔名>`
   (檔名中的 `/`、`:` 換成 `_`，加序號避免同名附件互相覆蓋；未下載到本機的附件存不出來，只列檔名)。
2. 圖片附件 (png/jpg/gif/webp/heic…，多半是簽名檔與內嵌圖) **不存檔、不轉換、只列檔名**。Exchange 信件的簽名圖與內嵌圖在郵件 App 中都算附件，所以這類「附件」會很多。
3. 其餘附件以 `docconvert --json --quiet <files>` 轉成 Markdown：PDF 走 `pdftotext`，Word 走 mammoth，
   Excel/CSV 走 SheetJS，簡報走 LibreOffice→PDF→pdftotext；一封信一次呼叫，單封失敗或逾時不影響其他信。
4. 每個附件截到 `MAIL_AGENT_MAX_ATTACHMENT_CHARS` 字元，以「附件：<檔名>」區塊接在信件內文後送進提示詞；
   轉換失敗、格式不支援、圖片等只在區塊標明原因，不送內容。
5. 分析結束 (不論成功失敗) 立刻刪除該次的暫存附件目錄；**轉換失敗的附件所在信件目錄保留**，報告尾端會列出路徑供除錯，確認後可整個刪掉。

**隱私**：docconvert 本身支援用視覺模型處理 PDF、簡報與圖片 (`--vlm`)，mail-agent **刻意永遠不加 `--vlm`**，
附件只做本機純文字抽取，附件內容與圖片不會送到任何視覺模型；抽出的文字跟信件內文一樣只送到你設定的 LLM 端點。
掃描件 PDF 因此只會得到「沒有可抽取的文字」，屬預期行為。

`--dry-run` 只列出附件檔名與大小，不存檔也不轉換。`MAIL_AGENT_SKIP_ATTACHMENTS=1` 可整個關閉附件處理。

## 注意事項

1. **系統授權**：首次執行時 macOS 會詢問「是否允許 Terminal 控制郵件」，請選擇「允許」。
   若誤按拒絕，到「系統設定 > 隱私權與安全性 > 自動化」重新開啟。
2. **帳號名稱**：`MAIL_AGENT_ACCOUNT` 必須與郵件 App 側邊欄顯示的名稱一致，可用 `--list-accounts` 確認。
   多個帳號請在同一次執行中以逗號指定，不要分次跑：`last_sync.json` 是所有帳號共用的，分次跑會讓後面的帳號漏信。
3. **收件夾名稱**：Exchange 帳號的收件夾在郵件 App 中可能叫 `Inbox` 或「收件匣」而非 `INBOX`，程式會自動嘗試常見名稱；
   若仍報「找不到收件夾」，錯誤訊息會列出該帳號實際的信箱名稱，將其設到 `MAIL_AGENT_MAILBOX` 即可。
4. **資料隱私**：只有「無旗標」且在「收件夾」中的信件文本，以及其附件在本機抽出的純文字 (截斷後) 會送至 LLM API，
   附件不經任何視覺模型 (見「附件」一節)，請確認符合公司 AI 工具使用規範。
5. **時間戳**：同步時間點在**抓信前**記錄，因此執行期間抵達的信件不會被漏掉，下次執行仍會處理。
6. **模型回覆解析失敗**：走 `prompt` 模式時，較小的模型 (例如 Apple Intelligence) 偶爾會輸出壞掉的 JSON。
   程式會先自動修復常見錯誤 (多餘的括號、未跳脫的引號、裸換行、被截斷的結尾)，修不好就重試一次；
   整批仍失敗時改為逐封分析，單封仍失敗則在報告中以「⚠️ 分析失敗」佔位並列出，不會中止整次執行，
   時間戳照常更新。壞掉的原始回覆存在 `~/.mail-agent/debug/` (可用 `MAIL_AGENT_DEBUG_DIR` 改)。
7. **首次執行很慢**：沒有 `last_sync.json` 時會掃描收件夾中所有未加旗標的信，且每封都要逐一透過 AppleScript 讀取內文。
   建議先手動建立起始時間點，例如只處理某個時間之後的信：

   ```bash
   printf '{\n  "last_time": "2026-10-26 08:00:00"\n}\n' > last_sync.json
   ```

   或先用 `--dry-run --limit 20` 小量確認設定正確。執行時畫面會即時顯示「已讀取 n/N 封」的進度。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
```
