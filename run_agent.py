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
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "last_sync.json"

# 郵件 App 側邊欄顯示的公司帳號名稱；可用環境變數 MAIL_AGENT_ACCOUNT 覆寫
TARGET_ACCOUNT = os.environ.get("MAIL_AGENT_ACCOUNT", "Exchange")
TARGET_MAILBOX = os.environ.get("MAIL_AGENT_MAILBOX", "INBOX")

# LLM 設定；OPENAI_BASE_URL 可指向 Azure OpenAI 或其他相容介面
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# 每封信內文最多送出的字元數、每次 LLM 呼叫最多幾封信
MAX_BODY_CHARS = int(os.environ.get("MAIL_AGENT_MAX_BODY_CHARS", "4000"))
BATCH_SIZE = int(os.environ.get("MAIL_AGENT_BATCH_SIZE", "10"))

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
def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript 執行失敗")
    return result.stdout


def list_accounts() -> List[str]:
    script = 'tell application "Mail" to return name of every account'
    out = run_applescript(script).strip()
    return [a.strip() for a in out.split(",") if a.strip()] if out else []


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


def fetch_unflagged_inbox_mails(
    account_name: str, since: Optional[datetime] = None, mailbox: str = TARGET_MAILBOX
) -> List[dict]:
    """讀取指定帳號 INBOX 中「無旗標」且 (若提供) 「晚於 since」的信件。"""
    acct = _applescript_string(account_name)
    mbox = _applescript_string(mailbox)
    script = f"""
    set RS to (ASCII character 30)
    set FS to (ASCII character 31)
    {_applescript_cutoff(since)}
    tell application "Mail"
        set targetAccount to account "{acct}"
        set targetMailbox to mailbox "{mbox}" of targetAccount
        if cutoffDate is missing value then
            set targetMessages to (every message of targetMailbox whose flagged status is false)
        else
            set targetMessages to (every message of targetMailbox whose flagged status is false and date received > cutoffDate)
        end if
        set output to ""
        repeat with m in targetMessages
            set mailSubject to subject of m
            set mailSender to sender of m
            set mailDate to (date received of m) as string
            set mailContent to ""
            try
                set mailContent to content of m
            end try
            set output to output & mailSubject & FS & mailSender & FS & mailDate & FS & mailContent & RS
        end repeat
        return output
    end tell
    """
    return parse_mail_output(run_applescript(script))


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


def analyze_emails_with_llm(emails: List[dict]) -> Optional[DailyBriefing]:
    if not emails:
        return None

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("請設定 OPENAI_API_KEY 環境變數！")

    from openai import OpenAI  # 延遲載入，讓 --list-accounts / --dry-run 不需安裝 openai

    client = OpenAI()  # 自動讀取 OPENAI_API_KEY / OPENAI_BASE_URL

    all_briefings: List[EmailSummary] = []
    for start in range(0, len(emails), BATCH_SIZE):
        batch = emails[start : start + BATCH_SIZE]
        prompt = (
            "請閱讀以下來自公司收件匣的最新郵件，為使用者進行重點摘要、"
            "萃取行動事項，並提供具體切實的應對方針建議。\n\n"
            f"郵件資料 (共 {len(batch)} 封):\n"
            f"{json.dumps(batch, ensure_ascii=False, indent=2)}"
        )
        completion = client.chat.completions.parse(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format=DailyBriefing,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("LLM 未回傳有效的結構化結果")
        all_briefings.extend(parsed.briefings)

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
        preview = m["body"].replace("\n", " ")[:120]
        print(f" 📄 內文預覽: {preview}")


# ---------------------------------------------------------------------------
# 主執行流程
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="macOS Mail Agent — Exchange 郵件 AI 幕僚")
    parser.add_argument("--list-accounts", action="store_true", help="列出郵件 App 中的帳號名稱後結束")
    parser.add_argument("--dry-run", action="store_true", help="只抓取信件並列出，不呼叫 LLM、不更新時間戳")
    parser.add_argument("--all", action="store_true", help="忽略上次執行時間，掃描所有無旗標信件")
    parser.add_argument("--account", default=TARGET_ACCOUNT, help=f"郵件帳號名稱 (預設: {TARGET_ACCOUNT})")
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

    last_sync = None if args.all else get_last_sync_time()
    print(
        "📅 上次處理時間: "
        + (last_sync.strftime(TIME_FORMAT) if last_sync else "無紀錄 (將掃描所有無旗標信件)")
    )

    # 在抓信「之前」記下本次時間點，避免漏掉執行期間抵達的信件
    run_started_at = datetime.now().replace(microsecond=0)

    print(f"📥 正在讀取 [{args.account}] 帳號的收件夾 ({TARGET_MAILBOX})...")
    try:
        raw_mails = fetch_unflagged_inbox_mails(args.account, since=last_sync)
    except RuntimeError as e:
        print(f"❌ 讀取郵件失敗: {e}")
        print("   提示: 請確認已允許 Terminal 控制「郵件」，並用 --list-accounts 確認帳號名稱。")
        return 1

    if not raw_mails:
        print("✨ 目前收件夾沒有需要處理的新信件！")
        return 0

    print(f"🔍 掃描到 {len(raw_mails)} 封未標記旗標的信件" + ("" if args.dry_run else "，正在進行 AI 幕僚分析..."))

    if args.dry_run:
        print_raw_mails(raw_mails)
        print("\n(dry-run 模式：未呼叫 LLM，也未更新同步時間點)")
        return 0

    try:
        report = analyze_emails_with_llm(raw_mails)
    except Exception as e:
        print(f"❌ 執行過程發生錯誤: {e}")
        return 1

    if report is None:
        print("❌ LLM 未產出報告")
        return 1

    print_report(report)
    save_last_sync_time(run_started_at)
    print(f"\n✅ 處理完成，已更新同步時間點為: {run_started_at.strftime(TIME_FORMAT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
