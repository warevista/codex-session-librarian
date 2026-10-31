from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import csl_live as live
import csl_media as media
import codex_sessions as csl
from csl_live_fixtures import text_case


class LiveSafetyTests(unittest.TestCase):
    def test_explicit_paid_optin_required_before_any_subprocess(self):
        with patch.object(live.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "allow-paid"):
            live.run(SimpleNamespace(allow_paid=False), None)
        run.assert_not_called()

    def test_personal_codex_home_rejected(self):
        args = SimpleNamespace(allow_paid=True, targets="codex", codex_home=Path.home() / ".codex")
        with patch.object(live.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "never ~/.codex"):
            live.run(args, None)
        run.assert_not_called()

    def test_claude_requires_explicit_api_key_and_no_implicit_keychain(self):
        args = SimpleNamespace(allow_paid=True, targets="claude", codex_home=None)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            live.run(args, None)

    def test_each_cli_receives_only_its_credential_family(self):
        env = {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "do-not-use", "CODEX_API_KEY": "do-not-use",
               "ANTHROPIC_API_KEY": "in-memory-only", "ANTHROPIC_CUSTOM_HEADERS": "workspace-value"}
        command, child = live.invoke("codex", "sid", Path("/test/codex"), Path("/test/project"), env, "model", Decimal("0.05"))
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("shell_tool", command)
        self.assertFalse(any("model_providers.openai" in value for value in command))
        command, child = live.invoke("claude", "sid", Path("/test/claude"), Path("/test/project"), env, "model", Decimal("0.05"))
        self.assertEqual(child["ANTHROPIC_API_KEY"], "in-memory-only")
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertIn("--bare", command)
        self.assertIn("--max-budget-usd", command)
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_ground_truth_not_leaked_in_recall_question(self):
        case = text_case("UNIQUE73")
        for value in case["expected"].values():
            if isinstance(value, str):
                self.assertNotIn(value, case["question"])
        answer = json.dumps(case["expected"])
        self.assertEqual(live.checks(answer, case["expected"]), [])
        value = dict(case["expected"], release="wrong")
        self.assertEqual(live.checks(json.dumps(value), case["expected"]), ["release"])

    def test_answers_types_fences_and_amount(self):
        self.assertEqual(live.checks('```json\n{"outcome_known":false}\n```', {"outcome_known": False}), [])
        self.assertEqual(live.checks('{"outcome_known":"false"}', {"outcome_known": False}), ["outcome_known"])
        self.assertEqual(live.checks('{"invoice_amount":273.4}', {"invoice_amount": "273.40"}), [])
        with self.assertRaises(ValueError):
            live.answer_json("not JSON")

    def test_reports_redact_and_are_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            live.write_report(path, {"error": "credential-should-not-persist sk-ant-example-secret"}, {"ANTHROPIC_API_KEY": "credential-should-not-persist"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("credential-should-not-persist", path.read_text())
            self.assertNotIn("sk-ant-example-secret", path.read_text())

    def test_media_interpreter_keeps_virtualenv_symlink(self):
        with patch.dict(os.environ, {"CSL_MEDIA_PYTHON": "/some/venv/bin/python"}):
            self.assertEqual(media.interpreter(), "/some/venv/bin/python")

    def test_run_stops_at_call_or_cost_reservation_limit_without_retry(self):
        original_run = live.subprocess.run
        def safe_process(command, **kwargs):
            if command == ["claude", "--version"]:
                return SimpleNamespace(stdout="Claude Code test", returncode=0)
            return original_run(command, **kwargs)
        for flag, value in (("--max-calls", "1"), ("--total-budget-usd", "0.05")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp:
                args = csl.build_parser().parse_args(["verify-live", "--allow-paid", "--targets", "claude",
                    "--out", str(Path(temp) / "new-run"), flag, value])
                result = {"answer": "{}", "provider_failure": False, "tool_calls": 0,
                          "reported_cost_usd": 0.001, "usage": {}, "exit_code": 0, "timed_out": False}
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-not-used"}), \
                     patch.object(live.shutil, "which", return_value="/not-executed/claude"), \
                     patch.object(live.subprocess, "run", side_effect=safe_process), \
                     patch.object(live, "live_call", return_value=result) as call, \
                     patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(ValueError):
                    live.run(args, csl)
                call.assert_called_once()
                report = json.loads((Path(temp) / "new-run/report.json").read_text())
                self.assertEqual(report["calls_attempted"], 1)
                self.assertEqual(report["live_continuation"], "incomplete")
