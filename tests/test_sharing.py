import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sessions as csl
import csl_sharing as sharing

REAL_GH_RUNNER = sharing.run_gh


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.repo = {"nameWithOwner": "team/private-exchange", "isPrivate": True,
                     "viewerPermission": "ADMIN"}
        self.calls = []
        self.releases = {}
        self.assets = {}
        self.patches = [
            patch.object(csl, "DEFAULT_CODEX_HOME", self.home),
            patch.object(csl, "DEFAULT_SESSIONS_DIR", self.home / "sessions"),
            patch.object(csl, "DEFAULT_SESSION_INDEX", self.home / "session_index.jsonl"),
            patch.object(csl, "DEFAULT_DB", self.home / "index.sqlite"),
            patch.object(sharing, "run_gh", side_effect=self.fake_gh),
        ]
        for item in self.patches:
            item.start()
        self.sid = self.make_session()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def make_session(self):
        sid = str(uuid.uuid4())
        directory = self.home / "sessions/2026/01/01"
        directory.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/source"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Synthetic session. Remember cobalt 73."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Cobalt 73 remembered."}]}},
        ]
        (directory / f"rollout-2026-01-01T00-00-00-{sid}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records))
        return sid

    def fake_gh(self, args):
        self.calls.append(args)
        if args[:2] == ["repo", "view"]:
            return json.dumps(self.repo)
        if args[:2] == ["release", "create"]:
            tag = args[2]
            data = Path(args[3]).read_bytes()
            notes = Path(args[args.index("--notes-file") + 1]).read_text()
            rid = len(self.releases) + 1
            self.releases[tag] = {"id": rid, "tag_name": tag, "draft": False,
                                  "name": args[args.index("--title") + 1], "body": notes,
                                  "author": {"login": "teammate"}, "published_at": "2026-01-01T00:00:00Z",
                                  "assets": [{"id": rid * 100, "name": sharing.ASSET, "size": len(data)}]}
            self.assets[tag] = data
            return "https://github.com/team/private-exchange/releases/tag/" + tag
        if args[:2] == ["release", "download"]:
            Path(args[args.index("--output") + 1]).write_bytes(self.assets[args[2]])
            return ""
        if args[0] == "api":
            endpoint = args[-1]
            if "/releases/tags/" in endpoint:
                return json.dumps(self.releases[endpoint.rsplit("/", 1)[1]])
            if "/releases?" in endpoint:
                return json.dumps(list(self.releases.values()))
        self.fail(f"Unexpected GitHub operation: {args}")

    def command(self, *argv):
        args = csl.build_parser().parse_args(list(argv))
        self.output = io.StringIO()
        with patch("sys.stdout", self.output):
            return args.func(args)

    def connect(self):
        self.command("team", "connect", "team/private-exchange")

    def publish(self):
        self.connect()
        return self.command("share", self.sid, "--title", "Test handoff")

    def receive(self, tag, *extra):
        return self.command("receive", tag, "--cwd", str(self.cwd), *extra)

    def imports(self):
        return [p for p in (self.home / "sessions").rglob("*.jsonl") if self.sid not in p.name]

    def test_connect_private_and_reject_public(self):
        self.connect()
        config = sharing.state_dir(csl) / "config.json"
        self.assertEqual(json.loads(config.read_text())["repo"], "team/private-exchange")
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.repo["isPrivate"] = False
        with self.assertRaises(SystemExit) as result:
            self.command("team", "connect", "team/public")
        self.assertEqual(result.exception.code, 2)
        self.assertEqual(json.loads(config.read_text())["repo"], "team/private-exchange")

    def test_share_inbox_receive_roundtrip(self):
        tag = self.publish()
        self.assertRegex(tag, r"^csl-[0-9a-f]{12}$")
        self.assertIn("recipient: csl receive", self.output.getvalue())
        self.command("inbox", "--json")
        entries = json.loads(self.output.getvalue())["shares"]
        self.assertEqual(entries[0]["id"], tag)
        self.assertEqual(entries[0]["author"], "teammate")
        self.assertFalse(any(c[:2] == ["release", "download"] for c in self.calls))
        ids = self.receive(tag)
        self.assertEqual(len(ids), 1)
        self.assertNotEqual(ids[0], self.sid)
        self.assertEqual(len(self.imports()), 1)
        title = csl.load_thread_names(csl.DEFAULT_SESSION_INDEX)[ids[0]]["thread_name"]
        self.assertEqual(title, "Test handoff")

    def test_receive_twice_reuses_local_copy_without_download(self):
        tag = self.publish()
        first = self.receive(tag)
        downloads = sum(c[:2] == ["release", "download"] for c in self.calls)
        second = self.receive(tag)
        self.assertEqual(first, second)
        self.assertEqual(len(self.imports()), 1)
        self.assertEqual(sum(c[:2] == ["release", "download"] for c in self.calls), downloads)

    def test_new_flag_creates_independent_copy(self):
        tag = self.publish()
        first = self.receive(tag)
        second = self.receive(tag, "--new")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.imports()), 2)

    def test_deleted_local_copy_is_downloaded_again(self):
        tag = self.publish()
        first = self.receive(tag)
        self.imports()[0].unlink()
        second = self.receive(tag)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.imports()), 1)

    def test_download_checksum_failure_installs_nothing(self):
        tag = self.publish()
        self.assets[tag] += b"damaged"
        with self.assertRaises(SystemExit):
            self.receive(tag)
        self.assertEqual(self.imports(), [])
        self.assertFalse((sharing.state_dir(csl) / "received.json").exists())

    def test_changed_remote_snapshot_rejected_even_with_new_flag(self):
        tag = self.publish()
        self.receive(tag)
        self.releases[tag]["assets"][0]["id"] += 1
        with self.assertRaises(SystemExit):
            self.receive(tag, "--new")
        self.assertEqual(len(self.imports()), 1)

    def test_visibility_change_blocks_all_operations(self):
        tag = self.publish()
        self.repo["isPrivate"] = False
        for argv in [("share", self.sid), ("inbox",), ("receive", tag)]:
            with self.assertRaises(SystemExit):
                self.command(*argv)

    def test_read_only_member_can_receive_but_not_publish(self):
        tag = self.publish()
        self.repo["viewerPermission"] = "READ"
        self.assertTrue(self.receive(tag))
        with self.assertRaises(SystemExit):
            self.command("share", self.sid)

    def test_receive_open_happens_after_receipt_is_saved(self):
        tag = self.publish()
        with patch.object(sharing.shutil, "which", return_value="/bin/codex"), \
             patch.object(sharing, "open_sessions") as launch:
            ids = self.receive(tag, "--open")
        launch.assert_called_once_with(ids, self.cwd, [], "codex")
        receipt = json.loads((sharing.state_dir(csl) / "received.json").read_text())
        self.assertEqual(next(iter(receipt.values()))["ids"], ids)

    def test_receive_forwards_open_flags_for_new_and_reused_sessions(self):
        tag = self.publish()
        flags = ["--dangerously-bypass-approvals-and-sandbox", "--model", "example-model"]
        with patch.object(sharing.shutil, "which", return_value="/bin/codex"), \
             patch.object(sharing, "open_sessions") as launch:
            ids = self.receive(tag, "--open", *flags)
            launch.assert_called_once_with(ids, self.cwd, flags, "codex")
            launch.reset_mock()
            self.assertEqual(self.receive(tag, "--open", *flags), ids)
            launch.assert_called_once_with(ids, self.cwd, flags, "codex")
        self.assertEqual(len(self.imports()), 1)

    def test_claude_share_receive_reuse_new_and_provider_discovery(self):
        from test_claude import fixture
        from csl_claude import target_path
        sid = str(uuid.uuid4())
        claude_home = self.root / "claude"
        source = claude_home / "projects/sender" / f"{sid}.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text("".join(json.dumps(row) + "\n" for row in fixture(sid)))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home), "CLAUDE_CODE_PROJECT_DIR_NAME": "recipient"}):
            self.connect()
            tag = self.command("share", sid, "--provider", "claude", "--title", "Claude handoff")
            self.assertEqual(self.command("inbox", "--json")[0]["provider"], "claude")
            with patch.object(sharing.shutil, "which", return_value="/bin/claude"), \
                 patch.object(sharing, "open_sessions") as launch:
                ids = self.receive(tag, "--open", "--dangerously-skip-permissions")
                launch.assert_called_once_with(ids, self.cwd, ["--dangerously-skip-permissions"], "claude")
            path = target_path(ids[0], self.cwd)
            self.assertTrue(path.is_file())
            self.assertEqual(self.receive(tag), ids)
            self.assertNotEqual(self.receive(tag, "--new"), ids)
            receipt = json.loads((sharing.state_dir(csl) / "received.json").read_text())
            self.assertEqual(next(iter(receipt.values()))["provider"], "claude")
            self.assertEqual(len(self.imports()), 0)
            self.assertEqual(json.loads(source.read_text().splitlines()[0])["sessionId"], sid)

    def test_cwd_change_requires_new_flag(self):
        tag = self.publish()
        self.receive(tag)
        with self.assertRaises(SystemExit):
            self.command("receive", tag, "--cwd", str(self.root))
        self.assertEqual(len(self.imports()), 1)

    def test_lock_prevents_concurrent_duplicate_imports(self):
        tag = self.publish()
        with sharing.receive_lock(csl), self.assertRaises(SystemExit):
            self.receive(tag)
        self.assertEqual(self.imports(), [])

    def test_short_share_id_supported(self):
        tag = self.publish()
        self.assertTrue(self.receive(tag.removeprefix("csl-")))

    def test_malformed_release_is_not_received_or_listed(self):
        tag = self.publish()
        self.releases[tag]["body"] = "not JSON"
        self.command("inbox", "--json")
        self.assertEqual(json.loads(self.output.getvalue())["shares"], [])
        with self.assertRaises(SystemExit):
            self.receive(tag)

    def test_metadata_package_mismatch_rejected(self):
        tag = self.publish()
        meta = json.loads(self.releases[tag]["body"])
        meta["sessions"][0]["title"] = "different"
        self.releases[tag]["body"] = json.dumps(meta)
        with self.assertRaises(SystemExit):
            self.receive(tag)
        self.assertEqual(self.imports(), [])

    def test_requires_connection_and_rejects_invalid_ids(self):
        with self.assertRaises(SystemExit):
            self.command("share", self.sid)
        self.connect()
        before = len(self.calls)
        with self.assertRaises(SystemExit):
            self.receive("../../anything")
        self.assertEqual(len(self.calls), before + 1)  # repo visibility check only

    def test_no_implicit_upload_of_credentials_or_project(self):
        self.publish()
        create, = [c for c in self.calls if c[:2] == ["release", "create"]]
        self.assertEqual(Path(create[3]).name, sharing.ASSET)
        self.assertIn("--notes-file", create)
        self.assertIn("--latest=false", create)
        self.assertNotIn("--generate-notes", create)
        self.assertFalse(any("git" in c for c in self.calls))

    def test_resharing_received_session_links_parent_snapshot(self):
        parent = self.publish()
        ids = self.receive(parent)
        child = self.command("share", ids[0], "--title", "Continuation")
        metadata = json.loads(self.releases[child]["body"])
        self.assertEqual(metadata["continues"], [parent])

    def test_gh_runner_uses_argv_and_handles_auth_errors(self):
        with patch.object(sharing.shutil, "which", return_value="/bin/gh"), \
             patch.object(sharing.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "Please authenticate"
            with self.assertRaises(sharing.TransferError):
                REAL_GH_RUNNER(["repo", "view"])
            self.assertEqual(run.call_args.args[0], ["gh", "repo", "view"])
            self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_multi_session_receive_imports_every_conversation(self):
        self.connect()
        second = self.make_session()
        tag = self.command("share", self.sid, second, "--title", "Two sessions")
        ids = self.receive(tag)
        self.assertEqual(len(ids), 2)
        self.assertNotIn(self.sid, ids)
        self.assertNotIn(second, ids)


if __name__ == "__main__":
    unittest.main()
