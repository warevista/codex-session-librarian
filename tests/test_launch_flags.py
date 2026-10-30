import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sessions as csl
import csl_transfer as transfer


class LaunchFlagsTests(unittest.TestCase):
    def parse(self, *argv):
        return csl.build_parser().parse_args(argv)

    def test_bare_open_remains_true_and_default_is_false(self):
        for prefix in [("receive", "csl-123456789abc"), ("import", "sample.csl.zip")]:
            args = self.parse(*prefix)
            self.assertFalse(args.open)
            self.assertEqual(args.open_args, [])
            args = self.parse(*prefix, "--open")
            self.assertTrue(args.open)
            self.assertEqual(args.open_args, [])

    def test_open_consumes_all_remaining_arguments_without_interpreting_them(self):
        flags = ["--dangerously-bypass-approvals-and-sandbox", "--model", "example-model",
                 "-c", 'custom="value with spaces"', "--help"]
        for prefix in [("receive", "csl-123456789abc"), ("import", "sample.csl.zip")]:
            args = self.parse(*prefix, "--cwd", "/tmp/project with spaces", "--open", *flags)
            self.assertEqual(args.cwd, Path("/tmp/project with spaces"))
            self.assertEqual(args.open_args, flags)

    def test_csl_options_before_open_still_apply(self):
        args = self.parse("receive", "csl-123456789abc", "--new", "--open", "--no-alt-screen")
        self.assertTrue(args.new)
        self.assertEqual(args.open_args, ["--no-alt-screen"])

    def test_csl_options_after_open_belong_to_child(self):
        args = self.parse("receive", "csl-123456789abc", "--open", "--new")
        self.assertFalse(args.new)
        self.assertEqual(args.open_args, ["--new"])

    def test_unknown_flags_without_open_still_fail(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("receive", "csl-123456789abc", "--dangerously-bypass-approvals-and-sandbox")

    def test_launch_passes_literal_argv_without_shell_and_propagates_exit(self):
        flags = ["--dangerously-bypass-approvals-and-sandbox", "-c", 'test="$(touch /tmp/not-executed); value"']
        with patch.object(transfer.subprocess, "call", return_value=7) as launch, self.assertRaises(SystemExit) as result:
            transfer.open_sessions(["new-session-uuid"], Path("/tmp/project with spaces"), flags)
        self.assertEqual(result.exception.code, 7)
        launch.assert_called_once_with(["codex", "-C", "/tmp/project with spaces", "resume", "--all",
                                        "new-session-uuid", *flags])


if __name__ == "__main__":
    unittest.main()
