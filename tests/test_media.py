import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import csl_canonical as canonical
import csl_content as content
import csl_media as media
from csl_live_fixtures import asset_digests, media_case


class MediaGuards(unittest.TestCase):
    def setUp(self):
        media.decode.cache_clear()
        self.addCleanup(media.decode.cache_clear)

    def test_external_assets_never_read_or_fetched(self):
        with patch.object(media.subprocess, "run") as run:
            for block in ({"type": "input_image", "image_url": "https://invalid.test/no-fetch"},
                          {"type": "image", "source": {"type": "url", "url": "file:///etc/passwd"}},
                          {"type": "input_file", "file_id": "file-unavailable"},
                          {"type": "document", "source": {"type": "url", "url": "http://localhost/no-fetch"}}):
                with self.subTest(block=block), self.assertRaises(ValueError):
                    content.blocks([block])
            run.assert_not_called()

    def test_base64_digest_mime_size_and_name_guards(self):
        for data in (None, "%%%%", "x" * 100):
            with self.assertRaises(ValueError):
                media.raw_bytes(data, 8)
        for mime in ("image/svg+xml", "image/tiff", "text/html"):
            with self.assertRaises(ValueError):
                media.image(mime, "")
        with self.assertRaises(ValueError):
            media.image("image/png", base64.b64encode(b"not PNG").decode())
        with self.assertRaises(ValueError):
            media.image("image/webp", base64.b64encode(b"RIFFxxxxWRONG").decode())
        for name in ("", "x\x00.pdf", "x" * 256):
            with self.assertRaises(ValueError):
                media.document(base64.b64encode(b"%PDF-1.7").decode(), name)
        with self.assertRaises(ValueError):
            media.document(base64.b64encode(b"%PDF-1.7").decode(), digest="wrong")

    def test_decoder_errors_missing_dependencies_and_timeouts(self):
        for stdout, code in ((b"bad JSON", 0), (b"[]", 0), (b'{"error":"bad"}', 2), (b'{"missing_dependency":true}', 2)):
            media.decode.cache_clear()
            with patch.object(media.subprocess, "run", return_value=SimpleNamespace(stdout=stdout, returncode=code)), self.assertRaises(ValueError):
                media.decode("pdf", "test", "application/pdf", sys.executable)
        with patch.object(media.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 15)), self.assertRaisesRegex(ValueError, "timed out"):
            media.decode("pdf", "test", "application/pdf", sys.executable)

    def test_worker_environment_omits_credentials_and_uses_isolated_cwd(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "should-not-leak", "OPENAI_API_KEY": "should-not-leak"}), \
             patch.object(media.subprocess, "run", return_value=SimpleNamespace(stdout=b"{}", returncode=0)) as run:
            media.decode("image", "AA==", "image/png", sys.executable)
        env = run.call_args.kwargs["env"]
        self.assertEqual(set(env), {"LANG", "HOME"})
        self.assertEqual(run.call_args.kwargs["cwd"], env["HOME"])
        self.assertIn("-I", run.call_args.args[0])

    def test_os_sandbox_disables_network_and_exposes_no_user_home(self):
        with patch.object(media.shutil, "which", return_value="/usr/bin/bwrap"):
            command = media.sandbox_command(sys.executable, "required")
        self.assertIn("--unshare-all", command)
        self.assertIn("--ro-bind", command)
        self.assertNotIn(str(Path.home()), command)
        self.assertIn("/csl_media_worker.py", command)
        with patch.object(media.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "required"):
            media.sandbox_command(sys.executable, "required")
        with self.assertRaises(ValueError):
            media.sandbox_command(sys.executable, "unknown")

    def test_pdf_decoder_response_shape_guards(self):
        block = media.document(base64.b64encode(b"%PDF-synthetic").decode())
        for reply in ({}, {"pages": None}, {"pages": []}, {"pages": [{}]},
                      {"pages": [{"text": "ok", "png": "not-base64"}]}):
            with patch.object(media, "decode", return_value=reply), self.assertRaises(ValueError):
                media.project(block)

    def test_output_publication_is_exclusive_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large"
            with self.assertRaisesRegex(ValueError, "64 MiB"):
                canonical.atomic_text(path, "x" * (64 * 1024 * 1024 + 1))
            self.assertFalse(path.exists())


@unittest.skipUnless(os.environ.get("CSL_RUN_MEDIA_TESTS") == "1", "Enable CSL_RUN_MEDIA_TESTS=1 with the optional media interpreter")
class EmbeddedMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.case = media_case("TEST73")

    def test_all_four_routes_preserve_original_bytes_and_semantics(self):
        for source, source_writer in (("codex", canonical.restore_codex), ("claude", canonical.restore_claude)):
            original = self.root / f"source-{source}.jsonl"
            source_writer(self.case["data"], original, self.root)
            before = canonical.normalize(source, original)
            for target, writer in (("codex", canonical.restore_codex), ("claude", canonical.restore_claude)):
                with self.subTest(route=f"{source}->{target}"):
                    path = self.root / f"{source}-{target}.jsonl"
                    writer(before, path, self.root)
                    after = canonical.normalize(target, path)
                    self.assertEqual(canonical.semantic_events(before), canonical.semantic_events(after))
                    self.assertEqual(asset_digests(before), asset_digests(after))
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_scan_has_no_text_layer_and_has_pixels(self):
        scan = self.case["data"]["events"][0]["blocks"][3]
        pages = media.project(scan)
        self.assertEqual(pages[0]["text"], "")
        self.assertTrue(base64.b64decode(pages[0]["png"]).startswith(b"\x89PNG"))

    def test_multipage_pdf_text_and_original_checksum(self):
        document = self.case["data"]["events"][0]["blocks"][2]
        pages = media.project(document)
        self.assertEqual(len(pages), 2)
        self.assertIn("PDF-TEST73", pages[0]["text"])
        self.assertIn("PAGE-TEST73", pages[1]["text"])
        self.assertEqual(hashlib.sha256(base64.b64decode(document["data"])).hexdigest(), document["sha256"])

    def test_tampered_projection_or_original_fails_closed(self):
        document = self.case["data"]["events"][0]["blocks"][2]
        projected, spans = content.encode([document], "codex")
        altered = copy.deepcopy(projected)
        altered[0]["text"] += "modified"
        with self.assertRaisesRegex(ValueError, "changed"):
            content.restore_spans(altered, spans)
        substituted = copy.deepcopy(spans)
        substituted[0]["block"] = self.case["data"]["events"][0]["blocks"][3]
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            content.restore_spans(projected, substituted)

    def test_pdf_forms_and_native_claude_document(self):
        document = self.case["data"]["events"][0]["blocks"][2]
        for native in ({"type": "input_file", "filename": document["name"], "file_data": "data:application/pdf;base64," + document["data"]},
                       {"type": "document", "title": document["name"], "source": {"type": "base64", "media_type": "application/pdf", "data": document["data"]}}):
            self.assertEqual(content.blocks([native]), [document])
        encoded = content.native([document], "claude")
        self.assertEqual(encoded[0]["type"], "document")
        self.assertEqual(encoded[0]["source"]["data"], document["data"])

    def test_corrupt_protected_and_too_many_pages_fail_without_output(self):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pdfencrypt import StandardEncryption
        protected, too_many = io.BytesIO(), io.BytesIO()
        pdf = canvas.Canvas(protected, encrypt=StandardEncryption("not-guessed"))
        pdf.drawString(10, 10, "protected fixture")
        pdf.save()
        pdf = canvas.Canvas(too_many)
        for _ in range(21):
            pdf.showPage()
        pdf.save()
        for raw in (b"%PDF-1.7\nbroken", protected.getvalue(), too_many.getvalue()):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                media.project(media.document(base64.b64encode(raw).decode()))
        malformed = copy.deepcopy(self.case["data"])
        malformed["events"][0]["blocks"][2] = media.document(base64.b64encode(b"%PDF-broken").decode())
        path = self.root / "not-published.jsonl"
        with self.assertRaises(ValueError):
            canonical.restore_codex(malformed, path, self.root)
        self.assertFalse(path.exists())

    def test_supported_image_formats_and_animation_rejection(self):
        from PIL import Image
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")):
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "red").save(output, format=fmt)
            self.assertEqual(media.image(mime, base64.b64encode(output.getvalue()).decode())["mime_type"], mime)
        output = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(output, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")])
        with self.assertRaises(ValueError):
            media.image("image/gif", base64.b64encode(output.getvalue()).decode())

    def test_invalid_dimensions_truncation_and_digest_rejected(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8193, 1), "red").save(output, format="PNG")
        with self.assertRaises(ValueError):
            media.image("image/png", base64.b64encode(output.getvalue()).decode())
        original = self.case["data"]["events"][0]["blocks"][1]
        with self.assertRaises(ValueError):
            media.image("image/png", original["data"], "wrong")
        truncated = base64.b64encode(base64.b64decode(original["data"])[:40]).decode()
        with self.assertRaises(ValueError):
            media.image("image/png", truncated)

    def test_source_assets_appear_in_canonical_json_schema(self):
        import jsonschema
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/canonical-v2.schema.json").read_text())
        jsonschema.validate(self.case["data"], schema)
