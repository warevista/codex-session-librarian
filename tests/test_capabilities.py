import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sessions as csl
import csl_capabilities as caps


class CapabilityTests(unittest.TestCase):
    def test_every_command_is_in_coverage_registry(self):
        parser = csl.build_parser()
        sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        self.assertEqual(set(sub.choices), set(caps.coverage()["commands"]))

    def test_document_matches_registry_exactly(self):
        doc = Path(__file__).resolve().parents[1] / "docs/PROVIDER_MATRIX.md"
        self.assertIn(caps.markdown(), doc.read_text())

    def test_json_command_and_cross_provider_limits(self):
        args = csl.build_parser().parse_args(["capabilities", "--json"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        report = json.loads(output.getvalue())
        self.assertEqual(report["commands"]["receive"]["claude"], "supported")
        self.assertEqual(report["commands"]["recall"]["claude"], "not-integrated")
        self.assertEqual(report["routes"]["codex->claude"], "experimental-text-tools")
        self.assertEqual(report["source_lookup"]["default"], "auto")
