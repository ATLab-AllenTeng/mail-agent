#!/usr/bin/env python3
"""macOS Mail Agent — Exchange 郵件 AI 幕僚

透過 macOS 內建「郵件」App (AppleScript) 讀取指定帳號 INBOX 中
「未加旗標」且「晚於上次執行時間」的信件，交由 LLM 產出摘要、
待辦事項與應對方針。

用法:
    python3 run_agent.py                 # 正常執行
    python3 run_agent.py --list-accounts # 列出郵件 App 中的帳號名稱
    python3 run_agent.py --dry-run       # 只抓信、不呼叫 LLM、不更新時間戳
    python3 run_agent.py --all           # 忽略上次執行時間，掃描所有無旗標信件
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "last_sync.json"
ENV_FILE = BASE_DIR / ".env"


def load_dotenv(path: Path = ENV_FILE, environ: Optional[dict] = None) -> int:
    """極簡 .env 載入 (避免多一個相依套件)。

    格式：KEY=VALUE，支援 # 註解、空行、export 前綴與成對引號。
    已存在的環境變數不覆寫，所以臨時用 `MAIL_AGENT_MODEL=x python3 run_agent.py` 仍可覆蓋 .env。
    回傳實際載入的變數數量。
    """
    env = os.environ if environ is None else environ
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in env:
            env[key] = value
            loaded += 1
    return loaded


load_dotenv()


def setting(name: str, default: str = "", fallback: Optional[str] = None) -> str:
    """讀取 MAIL_AGENT_* 設定；未設定時可退回通用名稱 (如 OPENAI_*)，再退回預設值。"""
    value = os.environ.get(name, "")
    if not value and fallback:
        value = os.environ.get(fallback, "")
    return value if value else default


# 郵件 App 側邊欄顯示的公司帳號名稱；多個帳號以逗號分隔
TARGET_ACCOUNT = setting("MAIL_AGENT_ACCOUNT", "Exchange")
# 收件夾名稱會因帳號類型與語系不同 (INBOX / Inbox / 收件匣)，程式會依序嘗試；
# 可用 MAIL_AGENT_MAILBOX 指定優先名稱，用 --list-mailboxes 查看實際名稱
TARGET_MAILBOX = setting("MAIL_AGENT_MAILBOX", "INBOX")
INBOX_CANDIDATES = ["INBOX", "Inbox", "收件匣", "收件夹", "受信"]

# LLM 設定：一律以 MAIL_AGENT_* 為準，刻意不直接讀全域的 OPENAI_*，避免與其他應用互相干擾；
# 只有在 MAIL_AGENT_* 完全未設定時才退回 OPENAI_* (向下相容)。
# MAIL_AGENT_BASE_URL 可指向自架 llm-router、Azure OpenAI 或其他 OpenAI 相容介面。
LLM_API_KEY = setting("MAIL_AGENT_API_KEY", fallback="OPENAI_API_KEY")
LLM_BASE_URL = setting("MAIL_AGENT_BASE_URL", fallback="OPENAI_BASE_URL")
LLM_MODEL = setting("MAIL_AGENT_MODEL", "gpt-4o", fallback="OPENAI_MODEL")

# 結構化輸出方式：
#   schema  使用 OpenAI Structured Outputs (response_format=json_schema)，只有 OpenAI 官方端點保證支援
#   prompt  在提示詞中要求模型輸出符合 JSON Schema 的純 JSON，再用 pydantic 驗證；相容任何 OpenAI 相容介面
#   auto    有設定 MAIL_AGENT_BASE_URL 時用 prompt，否則用 schema
JSON_MODE = setting("MAIL_AGENT_JSON_MODE", "auto").lower()

# 每封信內文最多送出的字元數、每次 LLM 呼叫最多幾封信
MAX_BODY_CHARS = int(os.environ.get("MAIL_AGENT_MAX_BODY_CHARS", "4000"))
BATCH_SIZE = int(os.environ.get("MAIL_AGENT_BATCH_SIZE", "10"))

# LLM 回覆解析失敗時，原始回覆存到這裡供診斷 (預設 ~/.mail-agent/debug)
DEBUG_DIR = Path(os.environ.get("MAIL_AGENT_DEBUG_DIR", str(Path.home() / ".mail-agent" / "debug")))

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# AppleScript 輸出用的分隔字元 (ASCII 30 = record separator, 31 = unit separator)
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


# ---------------------------------------------------------------------------
# Pydantic 結構化輸出定義
# ---------------------------------------------------------------------------
class EmailSummary(BaseModel):
    subject: str = Field(description="郵件主旨")
    sender: str = Field(description="寄件者")
    date_str: str = Field(description="收信時間")
    summary: str = Field(description="50字以內的核心摘要")
    action_items: List[str] = Field(description="需要進行的下一步動作/待辦清單")
    strategy_advice: str = Field(description="針對該郵件的專業應對方針與建議")


class DailyBriefing(BaseModel):
    total_processed: int
    briefings: List[EmailSummary]


# ---------------------------------------------------------------------------
# 時間戳記管理
# ---------------------------------------------------------------------------
def get_last_sync_time() -> Optional[datetime]:
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        raw = data.get("last_time")
        return datetime.strptime(raw, TIME_FORMAT) if raw else None
    except Exception:
        return None


def save_last_sync_time(sync_time: datetime) -> None:
    CONFIG_FILE.write_text(
        json.dumps({"last_time": sync_time.strftime(TIME_FORMAT)}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AppleScript 郵件擷取
# ---------------------------------------------------------------------------
PROGRESS_PREFIX = "MAILAGENT_PROGRESS "


def run_applescript(script: str, on_progress: Optional[Callable[[str], None]] = None) -> str:
    """執行 AppleScript 並回傳 stdout。

    腳本中的 `log "MAILAGENT_PROGRESS ..."` 會在執行期間即時寫到 stderr，
    這裡逐行讀取並透過 on_progress 回報；其餘 stderr 內容視為錯誤訊息。
    """
    proc = subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None and proc.stderr is not None

    stdout_chunks: List[str] = []

    def drain_stdout() -> None:
        stdout_chunks.append(proc.stdout.read())

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()

    error_lines: List[str] = []
    for line in proc.stderr:
        line = line.rstrip("\n")
        if line.startswith(PROGRESS_PREFIX):
            if on_progress:
                on_progress(line[len(PROGRESS_PREFIX):])
        elif line.strip():
            error_lines.append(line)

    reader.join()
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError("\n".join(error_lines).strip() or "osascript 執行失敗")
    return "".join(stdout_chunks)


def list_accounts() -> List[str]:
    script = 'tell application "Mail" to return name of every account'
    out = run_applescript(script).strip()
    return [a.strip() for a in out.split(",") if a.strip()] if out else []


def list_mailboxes(account_name: str) -> List[str]:
    acct = _applescript_string(account_name)
    script = f'tell application "Mail" to return name of every mailbox of account "{acct}"'
    out = run_applescript(script).strip()
    return [m.strip() for m in out.split(",") if m.strip()] if out else []


def _applescript_string(value: str) -> str:
    """把 Python 字串安全地放進 AppleScript 的雙引號字串中。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _applescript_cutoff(since: Optional[datetime]) -> str:
    """產生在 AppleScript 中建立 cutoffDate 的程式碼。"""
    if since is None:
        return "set cutoffDate to missing value"
    return f"""
        set cutoffDate to current date
        set day of cutoffDate to 1
        set year of cutoffDate to {since.year}
        set month of cutoffDate to {since.month}
        set day of cutoffDate to {since.day}
        set hours of cutoffDate to {since.hour}
        set minutes of cutoffDate to {since.minute}
        set seconds of cutoffDate to {since.second}
    """


def _applescript_list(values: List[str]) -> str:
    return "{" + ", ".join(f'"{_applescript_string(v)}"' for v in values) + "}"


def inbox_candidates(mailbox: str = TARGET_MAILBOX) -> List[str]:
    """使用者指定的名稱優先，其後依序是常見的收件夾名稱。"""
    names = [mailbox] + [c for c in INBOX_CANDIDATES if c != mailbox]
    return names


def fetch_unflagged_inbox_mails(
    account_name: str,
    since: Optional[datetime] = None,
    mailbox: str = TARGET_MAILBOX,
    include_body: bool = True,
    limit: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """讀取指定帳號收件夾中「無旗標」且 (若提供) 「晚於 since」的信件。

    include_body=False 時不讀取內文 (快很多，供 dry-run 使用)；
    limit 限制最多讀取幾封，依郵件 App 回傳的順序取前 N 封 (通常最新的在前)。
    """
    acct = _applescript_string(account_name)
    candidates = _applescript_list(inbox_candidates(mailbox))
    limit_value = int(limit) if limit and limit > 0 else 0
    body_expr = "content of m" if include_body else '""'
    script = f"""
    set RS to string id 30
    set FS to string id 31
    set inboxNames to {candidates}
    set limitCount to {limit_value}
    {_applescript_cutoff(since)}
    tell application "Mail"
        set targetAccount to account "{acct}"
        set targetMailbox to missing value
        repeat with mb in (every mailbox of targetAccount)
            if (name of mb) is in inboxNames then
                set targetMailbox to mb
                exit repeat
            end if
        end repeat
        if targetMailbox is missing value then
            set availableNames to name of every mailbox of targetAccount
            set AppleScript's text item delimiters to ", "
            error "帳號「{acct}」找不到收件夾，實際的信箱名稱有: " & (availableNames as string) & "。請用 MAIL_AGENT_MAILBOX 指定。"
        end if
        log "{PROGRESS_PREFIX}正在篩選 [" & (name of targetMailbox) & "] 中的信件..."
        if cutoffDate is missing value then
            set targetMessages to (every message of targetMailbox whose flagged status is false)
        else
            set targetMessages to (every message of targetMailbox whose flagged status is false and date received > cutoffDate)
        end if
        set totalCount to count of targetMessages
        if limitCount > 0 and totalCount > limitCount then
            log "{PROGRESS_PREFIX}符合條件 " & totalCount & " 封，依 --limit 只讀取前 " & limitCount & " 封"
            set totalCount to limitCount
        else
            log "{PROGRESS_PREFIX}符合條件 " & totalCount & " 封，開始讀取..."
        end if
        set output to ""
        set i to 0
        repeat with m in targetMessages
            if i ≥ totalCount then exit repeat
            set i to i + 1
            set mailSubject to subject of m
            set mailSender to sender of m
            set mailDate to (date received of m) as string
            set mailContent to ""
            try
                set mailContent to {body_expr}
            end try
            set output to output & mailSubject & FS & mailSender & FS & mailDate & FS & mailContent & RS
            if i mod 10 = 0 or i = totalCount then
                log "{PROGRESS_PREFIX}已讀取 " & i & "/" & totalCount & " 封"
            end if
        end repeat
        return output
    end tell
    """
    return parse_mail_output(run_applescript(script, on_progress=on_progress))


def parse_mail_output(raw: str) -> List[dict]:
    emails: List[dict] = []
    for record in raw.split(RECORD_SEP):
        if not record.strip():
            continue
        parts = record.split(FIELD_SEP)
        if len(parts) < 4:
            continue
        subject, sender, date_str = (p.strip() for p in parts[:3])
        body = FIELD_SEP.join(parts[3:]).strip()
        emails.append(
            {
                "subject": subject or "(無主旨)",
                "sender": sender,
                "date_str": date_str,
                "body": body[:MAX_BODY_CHARS],
            }
        )
    return emails


# ---------------------------------------------------------------------------
# LLM 分析模組
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是一位專業的高級企業幕僚，擅長商業郵件分析。"
    "請精準、客觀且具備戰略思維地產出報告，一律使用繁體中文。"
    "每封郵件都必須對應一筆 briefing，順序與輸入相同；"
    "subject、sender、date_str 請原樣照抄輸入內容。"
)


def resolve_json_mode(mode: str = JSON_MODE, base_url: Optional[str] = None) -> str:
    """決定實際使用的結構化輸出方式 ('schema' 或 'prompt')。"""
    if mode in ("schema", "prompt"):
        return mode
    if mode != "auto":
        raise ValueError(f"MAIL_AGENT_JSON_MODE 只能是 auto / schema / prompt，收到: {mode}")
    if base_url is None:
        base_url = LLM_BASE_URL
    return "prompt" if base_url.strip() else "schema"


def extract_json(text: str) -> str:
    """從模型回覆中取出 JSON 物件：去掉 markdown 圍欄與前後多餘文字。"""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise ValueError("回覆中找不到 JSON 物件")
    if end < start:
        end = len(text) - 1  # 沒有結尾的 }：輸出被截斷，交給 repair_json 補完
    return text[start : end + 1]


def repair_json(text: str, max_fixes: int = 50) -> str:
    """修復 LLM 常見的 JSON 錯誤，回傳能被 json.loads 接受的字串；修不好就拋出原始錯誤。

    處理的情況：字串內的裸換行 / 控制字元、尾端多餘的逗號、多出來的 ] 或 }、
    字串內未跳脫的引號、輸出被截斷而缺少的結尾括號。
    """
    candidate = text
    # 字串內的裸換行、tab 等控制字元 → 跳脫
    def _escape_controls(m: "re.Match[str]") -> str:
        return m.group(0).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    candidate = re.sub(r'"(?:[^"\\]|\\.)*"', _escape_controls, candidate, flags=re.S)
    # 尾端多餘的逗號：,] 或 ,}
    candidate = re.sub(r",\s*([\]}])", r"\1", candidate)

    last_error: Optional[json.JSONDecodeError] = None
    for _ in range(max_fixes):
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as e:
            last_error = e
            pos = e.pos
            msg = e.msg
            if msg.startswith("Extra data"):
                # 物件已完整，後面是多出來的字元 (常見：多一個 ] 或 })
                candidate = candidate[:pos].rstrip()
                continue
            if msg.startswith("Expecting") and pos < len(candidate) and candidate[pos] in "]}":
                # 不該出現的結尾括號 → 刪掉
                candidate = candidate[:pos] + candidate[pos + 1 :]
                continue
            if msg.startswith("Expecting") and 0 < pos <= len(candidate) and candidate[pos - 1] == '"' and pos < len(candidate):
                # 字串內未跳脫的引號：把前一個引號跳脫掉
                candidate = candidate[: pos - 1] + '\\"' + candidate[pos:]
                continue
            if msg.startswith("Unterminated string") or (msg.startswith("Expecting") and pos >= len(candidate)):
                # 輸出被截斷：補上缺少的結尾
                candidate = _close_truncated(candidate)
                continue
            break
    assert last_error is not None
    raise last_error


def _close_truncated(text: str) -> str:
    """依括號堆疊補上缺少的結尾 (給被截斷的 JSON 用)。"""
    stack: List[str] = []
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}" and stack:
            stack.pop()
    text = text.rstrip()
    if in_str:
        text += '"'
    text = re.sub(r",\s*$", "", text)
    return text + "".join(reversed(stack))


def parse_briefing(content: str) -> DailyBriefing:
    """從模型回覆取出 JSON、必要時修復，再驗證成 DailyBriefing。"""
    raw = extract_json(content)
    try:
        return DailyBriefing.model_validate_json(raw)
    except Exception:
        repaired = repair_json(raw)
        return DailyBriefing.model_validate_json(repaired)


def save_debug(label: str, content: str, error: Exception) -> Optional[Path]:
    """把解析失敗的原始回覆存檔供診斷；存不了就略過。"""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / f"llm-raw-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}.txt"
        path.write_text(f"# error: {error}\n\n{content}", encoding="utf-8")
        return path
    except Exception:
        return None


def _build_user_prompt(batch: List[dict]) -> str:
    return (
        "請閱讀以下來自公司收件匣的最新郵件，為使用者進行重點摘要、"
        "萃取行動事項，並提供具體切實的應對方針建議。\n\n"
        f"郵件資料 (共 {len(batch)} 封):\n"
        f"{json.dumps(batch, ensure_ascii=False, indent=2)}"
    )


def _call_schema_mode(client, batch: List[dict]) -> DailyBriefing:
    completion = client.chat.completions.parse(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(batch)},
        ],
        response_format=DailyBriefing,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError("LLM 未回傳有效的結構化結果")
    return parsed


def _call_prompt_mode(client, batch: List[dict], max_attempts: int = 2) -> DailyBriefing:
    schema = json.dumps(DailyBriefing.model_json_schema(), ensure_ascii=False)
    system = (
        SYSTEM_PROMPT
        + "\n\n輸出規則：只輸出一個符合以下 JSON Schema 的 JSON 物件，"
        "不要加 markdown 圍欄、不要加任何說明文字。\n"
        f"JSON Schema:\n{schema}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _build_user_prompt(batch)},
    ]
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        completion = client.chat.completions.create(model=LLM_MODEL, messages=messages, temperature=0)
        content = completion.choices[0].message.content or ""
        try:
            return parse_briefing(content)
        except Exception as e:  # JSON 修不好或欄位不符：存檔、把錯誤回饋給模型再試一次
            last_error = e
            save_debug(f"batch{len(batch)}-try{attempt}", content, e)
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"你的輸出無法解析：{e}\n請只輸出修正後、完整且合法的 JSON 物件。"},
            ]
    raise RuntimeError(f"LLM 回覆無法解析為結構化結果: {last_error}")


FAILED_MARK = "⚠️ 分析失敗"


def _failed_summary(mail: dict, error: Exception) -> EmailSummary:
    """單封信分析失敗時的佔位結果，讓報告仍列出這封信而不是整次中止。"""
    return EmailSummary(
        subject=mail["subject"],
        sender=mail["sender"],
        date_str=mail["date_str"],
        summary=f"{FAILED_MARK}：{str(error).splitlines()[0][:120]}",
        action_items=["請直接開啟原信閱讀處理"],
        strategy_advice="模型回覆無法解析，原始回覆已存到 debug 目錄 (mail-agent status 可查看路徑)。",
    )


def _analyze_batch(call, client, batch: List[dict], on_progress: Optional[Callable[[str], None]] = None) -> List[EmailSummary]:
    """整批分析；失敗時退化為逐封分析，單封仍失敗則放入佔位結果。"""
    try:
        return list(call(client, batch).briefings)
    except Exception as batch_error:
        if len(batch) == 1:
            if on_progress:
                on_progress(f"「{batch[0]['subject']}」分析失敗：{str(batch_error).splitlines()[0][:80]}")
            return [_failed_summary(batch[0], batch_error)]
        if on_progress:
            on_progress(f"整批 {len(batch)} 封解析失敗，改為逐封分析")
        results: List[EmailSummary] = []
        for mail in batch:
            results.extend(_analyze_batch(call, client, [mail], on_progress))
        return results


def analyze_emails_with_llm(
    emails: List[dict], on_progress: Optional[Callable[[str], None]] = None
) -> Optional[DailyBriefing]:
    if not emails:
        return None

    if not LLM_API_KEY:
        raise ValueError("請在專案的 .env 設定 MAIL_AGENT_API_KEY (可從 .env.example 複製)！")

    from openai import OpenAI  # 延遲載入，讓 --list-accounts / --dry-run 不需安裝 openai

    # 明確傳入，不讓 SDK 自己去讀全域 OPENAI_* 環境變數
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    mode = resolve_json_mode()
    call = _call_schema_mode if mode == "schema" else _call_prompt_mode

    all_briefings: List[EmailSummary] = []
    for start in range(0, len(emails), BATCH_SIZE):
        batch = emails[start : start + BATCH_SIZE]
        if on_progress and len(emails) > BATCH_SIZE:
            on_progress(f"分析第 {start + 1}–{start + len(batch)} 封 (共 {len(emails)} 封)")
        all_briefings.extend(_analyze_batch(call, client, batch, on_progress))

    return DailyBriefing(total_processed=len(all_briefings), briefings=all_briefings)


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
def print_report(report: DailyBriefing) -> None:
    print("\n" + "=" * 50)
    print(f"📋 郵件摘要與方針報告 (共 {report.total_processed} 封)")
    print("=" * 50)
    for idx, item in enumerate(report.briefings, 1):
        print(f"\n【{idx}】{item.subject}")
        print(f" 👤 寄件人: {item.sender}")
        print(f" 🕒 時間: {item.date_str}")
        print(f" 📝 摘要: {item.summary}")
        print(" 🎯 待辦事項:")
        if item.action_items:
            for act in item.action_items:
                print(f"    - {act}")
        else:
            print("    - (無)")
        print(f" 💡 應對方針: {item.strategy_advice}")
        print("-" * 50)


def print_raw_mails(mails: List[dict]) -> None:
    for idx, m in enumerate(mails, 1):
        print(f"\n【{idx}】{m['subject']}")
        print(f" 👤 寄件人: {m['sender']}")
        print(f" 🕒 時間: {m['date_str']}")
        if m["body"]:
            preview = m["body"].replace("\n", " ")[:120]
            print(f" 📄 內文預覽: {preview}")


# ---------------------------------------------------------------------------
# 主執行流程
# ---------------------------------------------------------------------------
def split_accounts(value: str) -> List[str]:
    """把逗號分隔的帳號字串拆成清單，去除空白與重複。"""
    seen: List[str] = []
    for name in value.split(","):
        name = name.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="macOS Mail Agent — Exchange 郵件 AI 幕僚")
    parser.add_argument("--list-accounts", action="store_true", help="列出郵件 App 中的帳號名稱後結束")
    parser.add_argument("--list-mailboxes", action="store_true", help="列出 --account 指定帳號底下的信箱名稱後結束")
    parser.add_argument("--dry-run", action="store_true", help="只抓取信件並列出，不呼叫 LLM、不更新時間戳")
    parser.add_argument("--all", action="store_true", help="忽略上次執行時間，掃描所有無旗標信件")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="每個帳號最多讀取 N 封信 (依郵件 App 順序取前 N 封)")
    parser.add_argument(
        "--account",
        default=TARGET_ACCOUNT,
        help=f"郵件帳號名稱，多個以逗號分隔 (預設: {TARGET_ACCOUNT})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    print("==========================================")
    print("🚀 macOS Mail Agent 郵件幕僚正在啟動...")
    print("==========================================")

    if args.list_accounts:
        try:
            accounts = list_accounts()
        except RuntimeError as e:
            print(f"❌ 無法讀取郵件 App 帳號: {e}")
            return 1
        print("📮 郵件 App 中的帳號:")
        for a in accounts:
            print(f"   - {a}")
        return 0

    if args.list_mailboxes:
        for account in split_accounts(args.account):
            try:
                names = list_mailboxes(account)
            except RuntimeError as e:
                print(f"❌ 無法讀取 [{account}] 的信箱: {e}")
                return 1
            print(f"📮 [{account}] 的信箱:")
            for n in names:
                print(f"   - {n}")
        return 0

    last_sync = None if args.all else get_last_sync_time()
    print(
        "📅 上次處理時間: "
        + (last_sync.strftime(TIME_FORMAT) if last_sync else "無紀錄 (將掃描所有無旗標信件)")
    )

    accounts = split_accounts(args.account)
    if not accounts:
        print("❌ 未指定任何帳號")
        return 1

    # 在抓信「之前」記下本次時間點，避免漏掉執行期間抵達的信件
    run_started_at = datetime.now().replace(microsecond=0)

    # 先把所有帳號的信都抓完，再進行分析
    mails_by_account: List[tuple[str, List[dict]]] = []
    for account in accounts:
        print(f"📥 正在讀取 [{account}] 帳號的收件夾 ({TARGET_MAILBOX})...")
        try:
            mails = fetch_unflagged_inbox_mails(
                account,
                since=last_sync,
                include_body=not args.dry_run,
                limit=args.limit,
                on_progress=lambda msg: print(f"   ⏳ {msg}", flush=True),
            )
        except RuntimeError as e:
            print(f"❌ 讀取 [{account}] 郵件失敗: {e}")
            print("   提示: 請確認已允許 Terminal 控制「郵件」，並用 --list-accounts / --list-mailboxes 確認名稱。")
            return 1
        print(f"   找到 {len(mails)} 封未標記旗標的新信件")
        if mails:
            mails_by_account.append((account, mails))

    total = sum(len(m) for _, m in mails_by_account)
    if total == 0:
        print("✨ 目前收件夾沒有需要處理的新信件！")
        return 0

    if args.dry_run:
        print(f"🔍 共掃描到 {total} 封未標記旗標的信件")
    else:
        endpoint = LLM_BASE_URL.strip() or "OpenAI 官方端點"
        print(f"🔍 共掃描到 {total} 封未標記旗標的信件，正在進行 AI 幕僚分析...")
        print(f"   🤖 模型: {LLM_MODEL} @ {endpoint} (輸出方式: {resolve_json_mode()})")

    if args.dry_run:
        for account, mails in mails_by_account:
            print(f"\n📮 帳號: {account} ({len(mails)} 封)")
            print_raw_mails(mails)
        print("\n(dry-run 模式：未讀取內文、未呼叫 LLM，也未更新同步時間點)")
        return 0

    reports: List[tuple[str, DailyBriefing]] = []
    for account, mails in mails_by_account:
        try:
            report = analyze_emails_with_llm(mails, on_progress=lambda msg: print(f"   ⏳ {msg}", flush=True))
        except Exception as e:
            print(f"❌ 分析 [{account}] 郵件時發生錯誤: {e}")
            return 1
        if report is None:
            print(f"❌ LLM 未產出 [{account}] 的報告")
            return 1
        reports.append((account, report))

    failed = []
    for account, report in reports:
        print(f"\n📮 帳號: {account}")
        print_report(report)
        failed.extend(f"[{account}] {b.subject}" for b in report.briefings if b.summary.startswith(FAILED_MARK))

    if failed:
        print(f"\n⚠️ 有 {len(failed)} 封信模型回覆無法解析，請直接開啟原信處理 (原始回覆存於 {DEBUG_DIR})：")
        for f in failed:
            print(f"   - {f}")

    save_last_sync_time(run_started_at)
    print(f"\n✅ 處理完成，已更新同步時間點為: {run_started_at.strftime(TIME_FORMAT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
