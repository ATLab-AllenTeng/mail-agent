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

        def fake_run(script):
            captured["script"] = script
            return f"S{FS}F{FS}D{FS}B{RS}"

        with mock.patch.object(run_agent, "run_applescript", fake_run):
            mails = run_agent.fetch_unflagged_inbox_mails("My Exchange", since=datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(mails, [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}])
        self.assertIn('account "My Exchange"', captured["script"])
        self.assertIn('set inboxNames to {"INBOX", "Inbox", "收件匣", "收件夹", "受信"}', captured["script"])
        self.assertIn("is in inboxNames", captured["script"])
        self.assertIn("flagged status is false and date received > cutoffDate", captured["script"])


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

    def test_dry_run_skips_llm_and_timestamp(self):
        mails = [{"subject": "S", "sender": "F", "date_str": "D", "body": "B"}]
        with mock.patch.object(run_agent, "fetch_unflagged_inbox_mails", return_value=mails), \
             mock.patch.object(run_agent, "analyze_emails_with_llm") as llm:
            self.assertEqual(run_agent.main(["--dry-run"]), 0)
        llm.assert_not_called()
        self.assertFalse(self.cfg.exists())

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


if __name__ == "__main__":
    unittest.main()
