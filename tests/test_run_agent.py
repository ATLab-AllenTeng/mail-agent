import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_agent  # noqa: E402

RS, FS = run_agent.RECORD_SEP, run_agent.FIELD_SEP


class ParseMailOutputTests(unittest.TestCase):
    def test_parses_multiple_records(self):
        raw = (
            f"主旨一{FS}Alice <a@x.com>{FS}2026年10月26日 09:00{FS}內文一{RS}"
            f"Subject 2{FS}bob@x.com{FS}Oct 26, 2026 10:00{FS}body, with, commas{RS}"
        )
        mails = run_agent.parse_mail_output(raw)
        self.assertEqual(len(mails), 2)
        self.assertEqual(mails[0]["subject"], "主旨一")
        self.assertEqual(mails[0]["sender"], "Alice <a@x.com>")
        self.assertEqual(mails[1]["body"], "body, with, commas")

    def test_empty_output(self):
        self.assertEqual(run_agent.parse_mail_output(""), [])
        self.assertEqual(run_agent.parse_mail_output("\n"), [])

    def test_body_truncated_and_empty_subject(self):
        raw = f"{FS}s@x.com{FS}d{FS}{'x' * (run_agent.MAX_BODY_CHARS + 50)}{RS}"
        mails = run_agent.parse_mail_output(raw)
        self.assertEqual(mails[0]["subject"], "(無主旨)")
        self.assertEqual(len(mails[0]["body"]), run_agent.MAX_BODY_CHARS)

    def test_skips_malformed_record(self):
        raw = f"only{FS}two{RS}a{FS}b{FS}c{FS}d{RS}"
        mails = run_agent.parse_mail_output(raw)
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0]["subject"], "a")


class LastSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "last_sync.json"
        self.patcher = mock.patch.object(run_agent, "CONFIG_FILE", self.cfg)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(run_agent.get_last_sync_time())

    def test_round_trip(self):
        t = datetime(2026, 10, 26, 9, 30, 15)
        run_agent.save_last_sync_time(t)
        self.assertEqual(json.loads(self.cfg.read_text())["last_time"], "2026-10-26 09:30:15")
        self.assertEqual(run_agent.get_last_sync_time(), t)

    def test_corrupt_file_returns_none(self):
        self.cfg.write_text("not json")
        self.assertIsNone(run_agent.get_last_sync_time())


class AppleScriptBuildTests(unittest.TestCase):
    def test_cutoff_script_contains_components(self):
        s = run_agent._applescript_cutoff(datetime(2026, 10, 26, 9, 30, 15))
        for frag in ("year of cutoffDate to 2026", "month of cutoffDate to 10",
                     "day of cutoffDate to 26", "hours of cutoffDate to 9",
                     "minutes of cutoffDate to 30", "seconds of cutoffDate to 15"):
            self.assertIn(frag, s)

    def test_inbox_candidates_prefers_configured_name(self):
        self.assertEqual(run_agent.inbox_candidates("收件匣")[:2], ["收件匣", "INBOX"])
        self.assertEqual(run_agent.inbox_candidates("INBOX")[0], "INBOX")
        self.assertEqual(run_agent.inbox_candidates("INBOX").count("INBOX"), 1)

    def test_list_mailboxes_parses_names(self):
        with mock.patch.object(run_agent, "run_applescript", return_value="Inbox, Sent Items, Drafts\n"):
            self.assertEqual(run_agent.list_mailboxes("Celxpert"), ["Inbox", "Sent Items", "Drafts"])

    def test_no_cutoff(self):
        self.assertIn("missing value", run_agent._applescript_cutoff(None))

    def test_string_escaping(self):
        self.assertEqual(run_agent._applescript_string('A "B" \\ C'), 'A \\"B\\" \\\\ C')

    def test_fetch_passes_script_and_parses(self):
        captured = {}

        def fake_run(script, on_progress=None):
            captured["script"] = script
            return f"S{FS}F{FS}D{FS}B{RS}"

        with mock.patch.object(run_agent, "run_applescript", fake_run):
            mails = run_agent.fetch_unflagged_inbox_mails("My Exchange", since=datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(mails, [{"subject": "S", "sender": "F", "date_str": "D", "body": "B", "attachments": []}])
        self.assertIn('account "My Exchange"', captured["script"])
        self.assertIn('set inboxNames to {"INBOX", "Inbox", "收件匣", "收件夹", "受信"}', captured["script"])
        self.assertIn("is in inboxNames", captured["script"])
        self.assertIn("flagged status is false and date received > cutoffDate", captured["script"])
        self.assertIn("set mailContent to content of m", captured["script"])
        self.assertIn("set limitCount to 0", captured["script"])

    def test_fetch_without_body_and_with_limit(self):
        captured = {}

        def fake_run(script, on_progress=None):
            captured["script"] = script
            if on_progress:
                on_progress("已讀取 1/1 封")
            return f"S{FS}F{FS}D{FS}{RS}"

        progress = []
        with mock.patch.object(run_agent, "run_applescript", fake_run):
            mails = run_agent.fetch_unflagged_inbox_mails(
                "X", include_body=False, limit=5, on_progress=progress.append)
        self.assertEqual(mails[0]["body"], "")
        self.assertIn('set mailContent to ""', captured["script"])
        self.assertNotIn("content of m", captured["script"])
        self.assertIn("set limitCount to 5", captured["script"])
        self.assertEqual(progress, ["已讀取 1/1 封"])


class RunAppleScriptTests(unittest.TestCase):
    """用 python 子行程模擬 osascript，驗證進度行與錯誤處理。"""

    def _fake_osascript(self, stderr_lines, stdout_text, exit_code):
        code = (
            "import sys\n"
            + "".join(f"sys.stderr.write({line!r} + '\\n'); sys.stderr.flush()\n" for line in stderr_lines)
            + f"sys.stdout.write({stdout_text!r})\n"
            + f"sys.exit({exit_code})\n"
        )
        real_popen = run_agent.subprocess.Popen

        def popen(args, **kw):
            return real_popen([sys.executable, "-c", code], **kw)

        return mock.patch.object(run_agent.subprocess, "Popen", popen)

    def test_progress_lines_are_reported_and_stdout_returned(self):
        progress = []
        with self._fake_osascript(
            [run_agent.PROGRESS_PREFIX + "a", run_agent.PROGRESS_PREFIX + "b"], "OUT", 0):
            out = run_agent.run_applescript("ignored", on_progress=progress.append)
        self.assertEqual(out, "OUT")
        self.assertEqual(progress, ["a", "b"])

    def test_error_excludes_progress_lines(self):
        with self._fake_osascript([run_agent.PROGRESS_PREFIX + "a", "boom happened"], "", 1):
            with self.assertRaises(RuntimeError) as ctx:
                run_agent.run_applescript("ignored")
        self.assertEqual(str(ctx.exception), "boom happened")


class JsonModeTests(unittest.TestCase):
    def test_resolve_explicit_modes(self):
        self.assertEqual(run_agent.resolve_json_mode("schema", base_url="http://x"), "schema")
        self.assertEqual(run_agent.resolve_json_mode("prompt", base_url=""), "prompt")

    def test_resolve_auto_by_base_url(self):
        self.assertEqual(run_agent.resolve_json_mode("auto", base_url=""), "schema")
        self.assertEqual(run_agent.resolve_json_mode("auto", base_url="http://127.0.0.1:9001/v1"), "prompt")

    def test_resolve_invalid_mode(self):
        with self.assertRaises(ValueError):
            run_agent.resolve_json_mode("weird", base_url="")

    def test_extract_json_strips_fences_and_prose(self):
        self.assertEqual(run_agent.extract_json('```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(run_agent.extract_json('好的，結果如下：{"a": {"b": 2}} 以上。'), '{"a": {"b": 2}}')
        with self.assertRaises(ValueError):
            run_agent.extract_json("沒有 json")


def _fake_client(contents):
    """模擬 OpenAI client：chat.completions.create 依序回傳 contents 中的文字。"""
    client = mock.MagicMock()
    responses = []
    for c in contents:
        r = mock.MagicMock()
        r.choices = [mock.MagicMock()]
        r.choices[0].message.content = c
        responses.append(r)
    client.chat.completions.create.side_effect = responses
    return client


VALID_JSON = (
    '{"total_processed": 1, "briefings": [{"subject": "S", "sender": "F", "date_str": "D", '
    '"summary": "sum", "action_items": ["do"], "strategy_advice": "adv"}]}'
)


class PromptModeTests(unittest.TestCase):
    batch = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]

    def test_parses_fenced_json(self):
        client = _fake_client(["```json\n" + VALID_JSON + "\n```"])
        report = run_agent._call_prompt_mode(client, self.batch)
        self.assertEqual(report.briefings[0].summary, "sum")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertIn("JSON Schema", kwargs["messages"][0]["content"])
        self.assertNotIn("response_format", kwargs)

    def test_retries_once_with_feedback(self):
        client = _fake_client(["不是 json", VALID_JSON])
        report = run_agent._call_prompt_mode(client, self.batch)
        self.assertEqual(report.total_processed, 1)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        second_messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertIn("無法解析", second_messages[-1]["content"])

    def test_gives_up_after_retries(self):
        client = _fake_client(["x", "y"])
        with self.assertRaises(RuntimeError):
            run_agent._call_prompt_mode(client, self.batch)

    def test_analyze_uses_prompt_mode_when_base_url_set(self):
        client = _fake_client([VALID_JSON])
        with mock.patch.object(run_agent, "LLM_API_KEY", "k"), \
             mock.patch.object(run_agent, "LLM_BASE_URL", "http://127.0.0.1:9001/v1"), \
             mock.patch.object(run_agent, "JSON_MODE", "auto"), \
             mock.patch("openai.OpenAI", return_value=client) as ctor:
            report = run_agent.analyze_emails_with_llm(self.batch)
        self.assertEqual(report.total_processed, 1)
        ctor.assert_called_once_with(api_key="k", base_url="http://127.0.0.1:9001/v1")
        client.chat.completions.create.assert_called_once()
        client.chat.completions.parse.assert_not_called()

    def test_analyze_requires_api_key(self):
        with mock.patch.object(run_agent, "LLM_API_KEY", ""):
            with self.assertRaises(ValueError):
                run_agent.analyze_emails_with_llm(self.batch)


class DotenvTests(unittest.TestCase):
    def test_parses_file_without_overriding_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text(
                "# 註解\n"
                "MAIL_AGENT_ACCOUNT=Celxpert,Keelgoal\n"
                "export MAIL_AGENT_MODEL=\"Mail-Briefing\"\n"
                "MAIL_AGENT_API_KEY='k=with=equals'\n"
                "\n"
                "BROKEN LINE\n"
                "MAIL_AGENT_BATCH_SIZE=5\n",
                encoding="utf-8",
            )
            env = {"MAIL_AGENT_BATCH_SIZE": "20"}
            loaded = run_agent.load_dotenv(path, environ=env)
        self.assertEqual(loaded, 3)
        self.assertEqual(env["MAIL_AGENT_ACCOUNT"], "Celxpert,Keelgoal")
        self.assertEqual(env["MAIL_AGENT_MODEL"], "Mail-Briefing")
        self.assertEqual(env["MAIL_AGENT_API_KEY"], "k=with=equals")
        self.assertEqual(env["MAIL_AGENT_BATCH_SIZE"], "20")

    def test_missing_file(self):
        self.assertEqual(run_agent.load_dotenv(Path("/nonexistent/.env"), environ={}), 0)

    def test_setting_prefers_mail_agent_over_fallback(self):
        env = {"OPENAI_MODEL": "gpt-4o-mini"}
        with mock.patch.dict(run_agent.os.environ, env, clear=True):
            self.assertEqual(run_agent.setting("MAIL_AGENT_MODEL", "gpt-4o", fallback="OPENAI_MODEL"), "gpt-4o-mini")
        env["MAIL_AGENT_MODEL"] = "Mail-Briefing"
        with mock.patch.dict(run_agent.os.environ, env, clear=True):
            self.assertEqual(run_agent.setting("MAIL_AGENT_MODEL", "gpt-4o", fallback="OPENAI_MODEL"), "Mail-Briefing")
        with mock.patch.dict(run_agent.os.environ, {}, clear=True):
            self.assertEqual(run_agent.setting("MAIL_AGENT_MODEL", "gpt-4o", fallback="OPENAI_MODEL"), "gpt-4o")


class RepairJsonTests(unittest.TestCase):
    def _ok(self, text):
        return json.loads(run_agent.repair_json(text))

    def test_valid_passthrough(self):
        self.assertEqual(self._ok('{"a": [1, 2]}'), {"a": [1, 2]})

    def test_extra_closing_bracket(self):
        # 實際案例：Apple Intelligence 在 briefings 結尾多了一個 ]
        self.assertEqual(self._ok('{"n": 2, "b": [{"a": 1}, {"c": "x"}]]}'), {"n": 2, "b": [{"a": 1}, {"c": "x"}]})
        self.assertEqual(self._ok('{"a": 1}}'), {"a": 1})

    def test_unescaped_quote_inside_string(self):
        self.assertEqual(self._ok('{"s": "回覆："方向 OK"，請修正", "x": 1}'), {"s": '回覆："方向 OK"，請修正', "x": 1})

    def test_raw_newline_and_trailing_comma(self):
        self.assertEqual(self._ok('{"s": "a\nb", "l": ["x",], "x": 1,}'), {"s": "a\nb", "l": ["x"], "x": 1})

    def test_truncated_output(self):
        self.assertEqual(self._ok('{"n": 1, "b": [{"s": "S", "items": ["a", "b"'), {"n": 1, "b": [{"s": "S", "items": ["a", "b"]}]})
        self.assertEqual(self._ok('{"s": "未結束的字串'), {"s": "未結束的字串"})

    def test_unrepairable_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            run_agent.repair_json("{this is not json at all")

    def test_parse_briefing_repairs(self):
        broken = VALID_JSON[:-1] + "]}"  # 多一個 ]
        report = run_agent.parse_briefing("```json\n" + broken + "\n```")
        self.assertEqual(report.briefings[0].subject, "S")


class BatchFallbackTests(unittest.TestCase):
    batch = [
        {"subject": "S1", "sender": "F", "date_str": "D", "body": "B"},
        {"subject": "S2", "sender": "F", "date_str": "D", "body": "B"},
    ]

    def _briefing(self, subject):
        return run_agent.DailyBriefing(total_processed=1, briefings=[run_agent.EmailSummary(
            subject=subject, sender="F", date_str="D", summary="ok", action_items=[], strategy_advice="adv")])

    def test_batch_failure_falls_back_to_single_mails(self):
        calls = []

        def call(client, batch):
            calls.append([m["subject"] for m in batch])
            if len(batch) > 1:
                raise RuntimeError("batch broken")
            return self._briefing(batch[0]["subject"])

        progress = []
        out = run_agent._analyze_batch(call, None, self.batch, progress.append)
        self.assertEqual([b.subject for b in out], ["S1", "S2"])
        self.assertEqual(calls, [["S1", "S2"], ["S1"], ["S2"]])
        self.assertTrue(any("逐封" in p for p in progress))

    def test_single_mail_failure_becomes_placeholder(self):
        def call(client, batch):
            if any(m["subject"] == "S2" for m in batch):
                raise RuntimeError("still broken")
            return self._briefing(batch[0]["subject"])

        out = run_agent._analyze_batch(call, None, self.batch)
        self.assertEqual(out[0].summary, "ok")
        self.assertTrue(out[1].summary.startswith(run_agent.FAILED_MARK))
        self.assertEqual(out[1].subject, "S2")

    def test_main_reports_failed_mails_and_still_saves_sync(self):
        report = run_agent.DailyBriefing(total_processed=2, briefings=[
            self._briefing("S1").briefings[0],
            run_agent._failed_summary(self.batch[1], RuntimeError("x")),
        ])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(run_agent, "CONFIG_FILE", Path(d) / "last_sync.json"), \
             mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=self.batch), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=report), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 0)
            self.assertTrue((Path(d) / "last_sync.json").exists())
        self.assertIn("有 1 封信模型回覆無法解析", out.getvalue())
        self.assertIn("S2", out.getvalue())

    def test_save_debug_writes_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_agent, "DEBUG_DIR", Path(d)):
            path = run_agent.save_debug("t", "raw content", RuntimeError("boom"))
            self.assertTrue(path.exists())
            self.assertIn("raw content", path.read_text())


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "last_sync.json"
        self.patcher = mock.patch.object(run_agent, "CONFIG_FILE", self.cfg)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_no_mail_does_not_touch_timestamp(self):
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=[]):
            self.assertEqual(run_agent.main([]), 0)
        self.assertFalse(self.cfg.exists())

    def test_dry_run_skips_llm_timestamp_and_body(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": ""}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails) as fetch, \
             mock.patch.object(run_agent, "analyze_emails_with_llm") as llm:
            self.assertEqual(run_agent.main(["--dry-run", "--limit", "3"]), 0)
        llm.assert_not_called()
        self.assertFalse(self.cfg.exists())
        self.assertFalse(fetch.call_args.kwargs["include_body"])
        self.assertEqual(fetch.call_args.kwargs["limit"], 3)

    def test_normal_run_fetches_body(self):
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=[]) as fetch:
            run_agent.main([])
        self.assertTrue(fetch.call_args.kwargs["include_body"])
        self.assertIsNone(fetch.call_args.kwargs["limit"])

    def test_success_updates_timestamp(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]
        report = run_agent.DailyBriefing(
            total_processed=1,
            briefings=[run_agent.EmailSummary(
                subject="S", sender="F", date_str="D", summary="sum",
                action_items=["do it"], strategy_advice="advice")],
        )
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=report):
            self.assertEqual(run_agent.main([]), 0)
        self.assertTrue(self.cfg.exists())
        self.assertIsNotNone(run_agent.get_last_sync_time())

    def test_llm_failure_keeps_timestamp(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", side_effect=RuntimeError("boom")):
            self.assertEqual(run_agent.main([]), 1)
        self.assertFalse(self.cfg.exists())

    def test_multiple_accounts_fetched_and_reported(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]
        report = run_agent.DailyBriefing(
            total_processed=1,
            briefings=[run_agent.EmailSummary(
                subject="S", sender="F", date_str="D", summary="sum",
                action_items=[], strategy_advice="advice")],
        )
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails) as fetch, \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=report) as llm:
            self.assertEqual(run_agent.main(["--account", "Celxpert, Keelgoal"]), 0)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["Celxpert", "Keelgoal"])
        self.assertEqual(llm.call_count, 2)
        self.assertTrue(self.cfg.exists())

    def test_second_account_failure_keeps_timestamp(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails",
                               side_effect=[mails, RuntimeError("no such account")]):
            self.assertEqual(run_agent.main(["--account", "A,B"]), 1)
        self.assertFalse(self.cfg.exists())

    def test_split_accounts(self):
        self.assertEqual(run_agent.split_accounts(" A , B,,A "), ["A", "B"])
        self.assertEqual(run_agent.split_accounts(""), [])

    def test_all_flag_ignores_last_sync(self):
        run_agent.save_last_sync_time(datetime(2026, 1, 1, 0, 0, 0))
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=[]) as fetch:
            run_agent.main(["--all"])
        self.assertIsNone(fetch.call_args.kwargs["since"])


GS, ASEP = run_agent.ATTACH_BLOCK_SEP, run_agent.ATTACH_SEP


def _att(name, size, path=""):
    return f"{name}{FS}{size}{FS}{path}"


class AttachmentParseTests(unittest.TestCase):
    def test_parses_attachment_block_after_body(self):
        raw = (
            f"S{FS}F{FS}D{FS}內文{FS}含分隔字元{GS}"
            f"{_att('報價單.pdf', 12345, '/tmp/run/Celxpert/1/1-報價單.pdf')}{ASEP}"
            f"{_att('image001.png', 2048, '/tmp/run/Celxpert/1/2-image001.png')}{ASEP}{RS}"
            f"S2{FS}F{FS}D{FS}純內文{GS}{RS}"
        )
        mails = run_agent.parse_mail_output(raw)
        self.assertEqual(len(mails), 2)
        self.assertEqual(mails[0]["body"], f"內文{FS}含分隔字元")
        self.assertEqual(mails[0]["attachments"], [
            {"name": "報價單.pdf", "size": 12345, "path": "/tmp/run/Celxpert/1/1-報價單.pdf", "status": "saved"},
            {"name": "image001.png", "size": 2048, "path": "/tmp/run/Celxpert/1/2-image001.png", "status": "saved"},
        ])
        self.assertEqual(mails[1]["body"], "純內文")
        self.assertEqual(mails[1]["attachments"], [])

    def test_unsaved_attachment_and_bad_size(self):
        atts = run_agent.parse_attachment_block(f"{_att('a.docx', 10)}{ASEP}{_att('', 'x')}{ASEP}")
        self.assertEqual(atts[0], {"name": "a.docx", "size": 10, "path": "", "status": "unsaved"})
        self.assertEqual(atts[1]["name"], "attachment-2")
        self.assertEqual(atts[1]["size"], 0)

    def test_old_format_without_block_still_parses(self):
        mails = run_agent.parse_mail_output(f"S{FS}F{FS}D{FS}B{RS}")
        self.assertEqual(mails[0]["body"], "B")
        self.assertEqual(mails[0]["attachments"], [])


class AttachmentAppleScriptTests(unittest.TestCase):
    def _script(self, **kwargs):
        captured = {}

        def fake_run(script, on_progress=None):
            captured["script"] = script
            return ""

        with mock.patch.object(run_agent, "run_applescript", fake_run):
            run_agent.fetch_unflagged_inbox_mails("X", **kwargs)
        return captured["script"]

    def test_saves_attachments_into_run_dir(self):
        s = self._script(attachment_dir=Path("/Users/me/.mail-agent/attachments/20260101-000000/Celxpert"))
        self.assertIn("repeat with att in (mail attachments of m)", s)
        self.assertIn('set mailDir to "/Users/me/.mail-agent/attachments/20260101-000000/Celxpert/" & i', s)
        self.assertIn("my ensureDir(mailDir)", s)
        self.assertIn('attIndex & "-" & (my safeName(attName))', s)
        self.assertIn("save att in (POSIX file savedPath)", s)
        self.assertIn("if not (my isImageName(attName)) then", s)
        self.assertIn('set imageExtensions to {".bmp", ".gif", ".heic", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}', s)
        self.assertIn("on isImageName(n)", s)
        self.assertIn("on safeName(n)", s)
        self.assertIn("on ensureDir(p)", s)
        self.assertIn("& mailContent & GS & attInfo & RS", s)

    def test_dry_run_lists_without_saving(self):
        s = self._script(include_body=False, attachment_dir=None)
        self.assertIn("mail attachments of m", s)
        self.assertIn("file size of att", s)
        self.assertNotIn("save att in", s)
        self.assertNotIn("ensureDir(mailDir)", s)

    def test_skip_attachments_emits_empty_block(self):
        s = self._script(list_attachments=False)
        self.assertNotIn("mail attachments", s)
        self.assertIn('set attInfo to ""', s)
        self.assertIn("& GS & attInfo & RS", s)

    def test_account_slug(self):
        self.assertEqual(run_agent.account_slug("Celxpert"), "Celxpert")
        self.assertEqual(run_agent.account_slug("My Exchange / 公司"), "My_Exchange_公司")
        self.assertEqual(run_agent.account_slug("///"), "account")


class DocconvertCommandTests(unittest.TestCase):
    def test_prefers_configured_executable(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "docconvert"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            self.assertEqual(run_agent.resolve_docconvert_command(str(exe), Path(d) / "none.mjs"), [str(exe)])

    def test_falls_back_to_node_script(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "docconvert.mjs"
            script.write_text("")
            with mock.patch.object(run_agent.shutil, "which",
                                   side_effect=lambda name: "/usr/local/bin/node" if name == "node" else None):
                cmd = run_agent.resolve_docconvert_command(str(Path(d) / "missing"), script)
        self.assertEqual(cmd, ["/usr/local/bin/node", str(script)])

    def test_none_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_agent.shutil, "which", return_value=None):
            self.assertIsNone(run_agent.resolve_docconvert_command(str(Path(d) / "missing"), Path(d) / "none.mjs"))
        self.assertIn("MAIL_AGENT_SKIP_ATTACHMENTS", run_agent.docconvert_missing_hint())


class ConvertAttachmentsTests(unittest.TestCase):
    def _completed(self, stdout="", stderr="", returncode=0):
        return run_agent.subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_calls_docconvert_without_vlm_and_maps_results(self):
        paths = ["/tmp/x/1/1-a.pdf", "/tmp/x/1/2-b.zip"]
        payload = {"files": [
            {"source": "/private/tmp/x/1/1-a.pdf", "converter": "pdf", "ok": True, "markdown": "# a\n\n內容", "warnings": ["w"]},
            {"source": "/private/tmp/x/1/2-b.zip", "converter": None, "ok": False, "markdown": "", "error": "不支援的副檔名「.zip」"},
        ]}
        calls = {}

        def fake_run(argv, **kw):
            calls["argv"], calls["kw"] = argv, kw
            return self._completed(stdout=json.dumps(payload))

        with mock.patch.object(run_agent.subprocess, "run", fake_run), \
             mock.patch.object(run_agent.os.path, "realpath", lambda p: p.replace("/private/tmp", "/tmp")):
            results = run_agent.convert_attachments(paths, command=["/bin/docconvert"], timeout=42)
        self.assertEqual(calls["argv"], ["/bin/docconvert", "--json", "--quiet", *paths])
        self.assertNotIn("--vlm", calls["argv"])
        self.assertEqual(calls["kw"]["timeout"], 42)
        self.assertTrue(results[paths[0]]["ok"])
        self.assertEqual(results[paths[0]]["markdown"], "# a\n\n內容")
        self.assertEqual(results[paths[0]]["warnings"], ["w"])
        self.assertFalse(results[paths[1]]["ok"])
        self.assertIsNone(results[paths[1]]["converter"])
        self.assertIn("不支援", results[paths[1]]["error"])

    def test_timeout_and_bad_output_do_not_raise(self):
        paths = ["/tmp/a.pdf"]
        with mock.patch.object(run_agent.subprocess, "run",
                               side_effect=run_agent.subprocess.TimeoutExpired(cmd="x", timeout=1)):
            res = run_agent.convert_attachments(paths, command=["dc"], timeout=1)
        self.assertFalse(res[paths[0]]["ok"])
        self.assertIn("1 秒", res[paths[0]]["error"])
        with mock.patch.object(run_agent.subprocess, "run", return_value=self._completed(stdout="not json", stderr="boom")):
            res = run_agent.convert_attachments(paths, command=["dc"])
        self.assertIn("不是預期的 JSON", res[paths[0]]["error"])
        self.assertIn("boom", res[paths[0]]["error"])
        with mock.patch.object(run_agent.subprocess, "run", side_effect=FileNotFoundError("dc")):
            res = run_agent.convert_attachments(paths, command=["dc"])
        self.assertIn("無法執行", res[paths[0]]["error"])

    def test_missing_converter(self):
        with mock.patch.object(run_agent, "resolve_docconvert_command", return_value=None):
            res = run_agent.convert_attachments(["/tmp/a.pdf"])
        self.assertEqual(res["/tmp/a.pdf"]["error"], "找不到 docconvert")
        self.assertEqual(run_agent.convert_attachments([], command=["dc"]), {})

    def test_strip_title_line(self):
        self.assertEqual(run_agent.strip_title_line("# 1-報價.pdf\n\n內容", "/tmp/1/1-報價.pdf"), "\n內容")
        self.assertEqual(run_agent.strip_title_line("# 其他標題\n內容", "/tmp/1/1-報價.pdf"), "# 其他標題\n內容")
        self.assertEqual(run_agent.strip_title_line("", "/tmp/1/1-a.pdf"), "")

    def test_truncate_text(self):
        self.assertEqual(run_agent.truncate_text("  short  ", 10), "short")
        out = run_agent.truncate_text("x" * 50, 10)
        self.assertTrue(out.startswith("x" * 10 + "\n…"))
        self.assertIn("原長 50 字", out)
        self.assertEqual(run_agent.truncate_text("x" * 50, 0), "x" * 50)


class AttachAttachmentContentsTests(unittest.TestCase):
    def _mail(self, atts):
        return {"subject": "S", "sender": "F", "date_str": "D", "body": "B", "attachments": atts}

    def test_statuses_and_truncation(self):
        mail = self._mail([
            {"name": "a.pdf", "size": 1, "path": "/tmp/1/1-a.pdf", "status": "saved"},
            {"name": "logo.PNG", "size": 1, "path": "/tmp/1/2-logo.PNG", "status": "saved"},
            {"name": "missing.docx", "size": 1, "path": "", "status": "unsaved"},
            {"name": "x.zip", "size": 1, "path": "/tmp/1/4-x.zip", "status": "saved"},
            {"name": "bad.xlsx", "size": 1, "path": "/tmp/1/5-bad.xlsx", "status": "saved"},
        ])
        converted = {
            "/tmp/1/1-a.pdf": {"ok": True, "markdown": "y" * 100, "error": "", "warnings": [], "converter": "pdf"},
            "/tmp/1/4-x.zip": {"ok": False, "markdown": "", "error": "不支援的副檔名「.zip」", "warnings": [], "converter": None},
            "/tmp/1/5-bad.xlsx": {"ok": False, "markdown": "", "error": "檔頭不是 zip", "warnings": [], "converter": "sheet"},
        }
        progress = []
        with mock.patch.object(run_agent, "convert_attachments", return_value=converted) as conv:
            run_agent.attach_attachment_contents([mail], command=["dc"], max_chars=20, on_progress=progress.append)
        conv.assert_called_once_with(["/tmp/1/1-a.pdf", "/tmp/1/4-x.zip", "/tmp/1/5-bad.xlsx"], command=["dc"])
        statuses = [a["status"] for a in mail["attachments"]]
        self.assertEqual(statuses, ["converted", "image", "unsaved", "unsupported", "failed"])
        self.assertTrue(mail["attachments"][0]["content"].startswith("y" * 20))
        self.assertIn("已截斷", mail["attachments"][0]["content"])
        self.assertEqual(mail["attachments"][4]["error"], "檔頭不是 zip")
        self.assertTrue(progress and "1 封" in progress[0] and "3 個附件" in progress[0])

    def test_image_without_path_is_image_not_unsaved(self):
        mail = self._mail([{"name": "image001.PNG", "size": 1, "path": "", "status": "unsaved"}])
        with mock.patch.object(run_agent, "convert_attachments") as conv:
            run_agent.attach_attachment_contents([mail], command=["dc"])
        conv.assert_not_called()
        self.assertEqual(mail["attachments"][0]["status"], "image")

    def test_no_converter_marks_without_calling(self):
        mail = self._mail([{"name": "a.pdf", "size": 1, "path": "/tmp/1/1-a.pdf", "status": "saved"}])
        with mock.patch.object(run_agent, "convert_attachments") as conv:
            run_agent.attach_attachment_contents([mail], command=None)
        conv.assert_not_called()
        self.assertEqual(mail["attachments"][0]["status"], "no-converter")


class AttachmentPromptTests(unittest.TestCase):
    def test_mail_for_prompt_appends_blocks_without_paths(self):
        mail = {
            "subject": "S", "sender": "F", "date_str": "D", "body": "內文",
            "attachments": [
                {"name": "報價.pdf", "size": 2048, "path": "/secret/path/1-報價.pdf", "status": "converted", "content": "# 報價\n金額 100"},
                {"name": "logo.png", "size": 10, "path": "/secret/path/2-logo.png", "status": "image"},
                {"name": "bad.xlsx", "size": 10, "path": "/secret/path/3-bad.xlsx", "status": "failed", "error": "x"},
            ],
        }
        out = run_agent.mail_for_prompt(mail)
        self.assertEqual(set(out), {"subject", "sender", "date_str", "body"})
        self.assertIn("內文\n\n附件：報價.pdf (2.0 KB)\n# 報價\n金額 100", out["body"])
        self.assertIn("附件：logo.png (10 B) — 圖片，依隱私政策不讀取內容", out["body"])
        self.assertIn("附件：bad.xlsx (10 B) — 內容擷取失敗", out["body"])
        self.assertNotIn("/secret/path", out["body"])

    def test_mail_without_attachments_unchanged(self):
        mail = {"subject": "S", "sender": "F", "date_str": "D", "body": "B"}
        self.assertEqual(run_agent.mail_for_prompt(mail), mail)
        prompt = run_agent._build_user_prompt([mail])
        self.assertNotIn("附件", prompt)

    def test_user_prompt_mentions_attachment_blocks(self):
        mail = {"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                "attachments": [{"name": "a.pdf", "size": 1, "path": "/p", "status": "converted", "content": "C"}]}
        prompt = run_agent._build_user_prompt([mail])
        self.assertIn("「附件：<檔名>」區塊", prompt)
        self.assertIn("附件：a.pdf", prompt)
        self.assertNotIn('"/p"', prompt)

    def test_format_size(self):
        self.assertEqual(run_agent.format_size(512), "512 B")
        self.assertEqual(run_agent.format_size(1536), "1.5 KB")
        self.assertEqual(run_agent.format_size(3 * 1024 * 1024), "3.0 MB")


class CleanupAttachmentDirTests(unittest.TestCase):
    def test_removes_run_dir_but_keeps_failed_mail_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "20260101-000000"
            ok_dir, bad_dir = run_dir / "Celxpert" / "1", run_dir / "Celxpert" / "2"
            ok_dir.mkdir(parents=True)
            bad_dir.mkdir(parents=True)
            (ok_dir / "1-a.pdf").write_text("x")
            (bad_dir / "1-b.xlsx").write_text("x")
            (bad_dir / "2-c.pdf").write_text("x")
            mails = [("Celxpert", [
                {"attachments": [{"name": "a.pdf", "path": str(ok_dir / "1-a.pdf"), "status": "converted"}]},
                {"attachments": [
                    {"name": "b.xlsx", "path": str(bad_dir / "1-b.xlsx"), "status": "failed"},
                    {"name": "c.pdf", "path": str(bad_dir / "2-c.pdf"), "status": "converted"},
                ]},
            ])]
            kept = run_agent.cleanup_attachment_dir(run_dir, mails)
            self.assertEqual(kept, [bad_dir])
            self.assertFalse(ok_dir.exists())
            self.assertTrue((bad_dir / "1-b.xlsx").exists())
            self.assertTrue((bad_dir / "2-c.pdf").exists())
            self.assertTrue(run_dir.exists())

    def test_all_ok_removes_everything(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            (run_dir / "A" / "1").mkdir(parents=True)
            (run_dir / "A" / "1" / "1-a.pdf").write_text("x")
            mails = [("A", [{"attachments": [{"name": "a.pdf", "path": str(run_dir / "A/1/1-a.pdf"), "status": "converted"}]}])]
            self.assertEqual(run_agent.cleanup_attachment_dir(run_dir, mails), [])
            self.assertFalse(run_dir.exists())
            self.assertEqual(run_agent.cleanup_attachment_dir(run_dir, mails), [])  # 已不存在也不報錯


class AttachmentMainFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patchers = [
            mock.patch.object(run_agent, "CONFIG_FILE", self.root / "last_sync.json"),
            mock.patch.object(run_agent, "ATTACHMENT_ROOT", self.root / "attachments"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def _report(self):
        return run_agent.DailyBriefing(total_processed=1, briefings=[run_agent.EmailSummary(
            subject="S", sender="F", date_str="D", summary="sum", action_items=[], strategy_advice="adv")])

    def test_dry_run_lists_attachments_without_saving(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "",
                  "attachments": [{"name": "報價.pdf", "size": 2048, "path": "", "status": "unsaved"}]}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails) as fetch, \
             mock.patch.object(run_agent, "convert_attachments") as conv, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--dry-run", "--account", "Celxpert"]), 0)
        conv.assert_not_called()
        self.assertTrue(fetch.call_args.kwargs["list_attachments"])
        self.assertIsNone(fetch.call_args.kwargs["attachment_dir"])
        self.assertIn("報價.pdf (2.0 KB)", out.getvalue())
        self.assertIn("共 1 個附件", out.getvalue())

    def test_run_saves_converts_and_cleans_up(self):
        def fake_fetch(account, **kwargs):
            mail_dir = kwargs["attachment_dir"] / "1"
            mail_dir.mkdir(parents=True)
            (mail_dir / "1-a.pdf").write_text("pdf")
            return [{"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                     "attachments": [{"name": "a.pdf", "size": 3, "path": str(mail_dir / "1-a.pdf"), "status": "saved"}]}]

        def fake_convert(paths, command=None, timeout=None):
            return {p: {"ok": True, "markdown": "內容", "error": "", "warnings": [], "converter": "pdf"} for p in paths}

        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", side_effect=fake_fetch) as fetch, \
             mock.patch.object(run_agent, "resolve_docconvert_command", return_value=["dc"]), \
             mock.patch.object(run_agent, "convert_attachments", side_effect=fake_convert), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=self._report()) as llm, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 0)
        attachment_dir = fetch.call_args.kwargs["attachment_dir"]
        self.assertEqual(attachment_dir.parent.parent, self.root / "attachments")
        self.assertEqual(attachment_dir.name, "Celxpert")
        sent = llm.call_args.args[0]
        self.assertEqual(sent[0]["attachments"][0]["status"], "converted")
        self.assertEqual(sent[0]["attachments"][0]["content"], "內容")
        self.assertFalse((self.root / "attachments").exists() and any((self.root / "attachments").iterdir()))
        self.assertIn("已擷取內容 1", out.getvalue())
        self.assertTrue((self.root / "last_sync.json").exists())

    def test_failed_conversion_is_kept_and_reported(self):
        def fake_fetch(account, **kwargs):
            mail_dir = kwargs["attachment_dir"] / "1"
            mail_dir.mkdir(parents=True)
            (mail_dir / "1-b.xlsx").write_text("x")
            return [{"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                     "attachments": [{"name": "b.xlsx", "size": 1, "path": str(mail_dir / "1-b.xlsx"), "status": "saved"}]}]

        def fake_convert(paths, command=None, timeout=None):
            return {p: {"ok": False, "markdown": "", "error": "檔頭不是 zip", "warnings": [], "converter": "sheet"} for p in paths}

        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", side_effect=fake_fetch) as fetch, \
             mock.patch.object(run_agent, "resolve_docconvert_command", return_value=["dc"]), \
             mock.patch.object(run_agent, "convert_attachments", side_effect=fake_convert), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=self._report()), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 0)
        kept = fetch.call_args.kwargs["attachment_dir"] / "1" / "1-b.xlsx"
        self.assertTrue(kept.exists())
        self.assertIn("擷取失敗 1", out.getvalue())
        self.assertIn("b.xlsx: 檔頭不是 zip", out.getvalue())
        self.assertIn(str(kept.parent), out.getvalue())

    def test_missing_docconvert_lists_names_only(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                  "attachments": [{"name": "a.pdf", "size": 1, "path": str(self.root / "a.pdf"), "status": "saved"}]}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails), \
             mock.patch.object(run_agent, "resolve_docconvert_command", return_value=None), \
             mock.patch.object(run_agent, "convert_attachments") as conv, \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=self._report()) as llm, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 0)
        conv.assert_not_called()
        self.assertEqual(llm.call_args.args[0][0]["attachments"][0]["status"], "no-converter")
        self.assertIn("找不到 docconvert", out.getvalue())
        self.assertIn("未安裝 docconvert 1", out.getvalue())

    def test_all_images_do_not_need_docconvert(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                  "attachments": [{"name": "image001.png", "size": 1, "path": "", "status": "unsaved"}]}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails), \
             mock.patch.object(run_agent, "resolve_docconvert_command") as resolve, \
             mock.patch.object(run_agent, "analyze_emails_with_llm", return_value=self._report()), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 0)
        resolve.assert_not_called()
        self.assertIn("全部是圖片或無法取出", out.getvalue())
        self.assertNotIn("找不到 docconvert", out.getvalue())
        self.assertIn("圖片略過 1", out.getvalue())

    def test_skip_attachments_env(self):
        with mock.patch.object(run_agent, "SKIP_ATTACHMENTS", True), \
             mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=[]) as fetch, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            run_agent.main([])
        self.assertFalse(fetch.call_args.kwargs["list_attachments"])
        self.assertIsNone(fetch.call_args.kwargs["attachment_dir"])
        self.assertIn("MAIL_AGENT_SKIP_ATTACHMENTS", out.getvalue())

    def test_llm_failure_still_cleans_attachments(self):
        def fake_fetch(account, **kwargs):
            mail_dir = kwargs["attachment_dir"] / "1"
            mail_dir.mkdir(parents=True)
            (mail_dir / "1-a.pdf").write_text("pdf")
            return [{"subject": "S", "sender": "F", "date_str": "D", "body": "B",
                     "attachments": [{"name": "a.pdf", "size": 3, "path": str(mail_dir / "1-a.pdf"), "status": "saved"}]}]

        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", side_effect=fake_fetch), \
             mock.patch.object(run_agent, "resolve_docconvert_command", return_value=None), \
             mock.patch.object(run_agent, "analyze_emails_with_llm", side_effect=RuntimeError("boom")), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(run_agent.main(["--account", "Celxpert"]), 1)
        self.assertEqual(list((self.root / "attachments").iterdir()), [])  # run 目錄已刪，只剩空的根目錄
        self.assertFalse((self.root / "last_sync.json").exists())



if __name__ == "__main__":
    unittest.main()
