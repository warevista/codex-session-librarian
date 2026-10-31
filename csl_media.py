"""Standard-library bridge to an optional, resource-bounded media decoder."""

import base64
import binascii
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import warnings

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PROJECTED_BYTES = 32 * 1024 * 1024
SIGNATURES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
              "image/gif": b"GIF8", "image/webp": b"RIFF"}


def interpreter():
    explicit = os.environ.get("CSL_MEDIA_PYTHON")
    if explicit:
        # Resolving the executable symlink would bypass a virtualenv's site-packages.
        return str(Path(explicit).expanduser().absolute())
    installed = Path.home() / ".local/share/csl/media-venv/bin/python"
    return str(installed) if installed.is_file() else sys.executable


def raw_bytes(data, limit):
    if not isinstance(data, str) or len(data) > limit * 4 // 3 + 8:
        raise ValueError("Embedded asset base64/size invalid")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid embedded asset base64") from exc
    if len(raw) > limit:
        raise ValueError("Embedded asset exceeds byte limit")
    return raw


def sandbox_command(python, mode):
    if mode not in ("auto", "required", "process"):
        raise ValueError("CSL_MEDIA_SANDBOX must be auto, required or process")
    worker = str(Path(__file__).with_name("csl_media_worker.py"))
    bwrap = shutil.which("bwrap")
    if mode == "process" or not bwrap:
        if mode == "required":
            raise ValueError("bubblewrap is required by CSL_MEDIA_SANDBOX=required")
        if mode == "auto":
            warnings.warn("bubblewrap unavailable: media has process/resource isolation only; require it for untrusted input", RuntimeWarning)
        return [python, "-I", worker]
    command = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--ro-bind", "/usr", "/usr",
               "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
               "--ro-bind-try", "/etc/fonts", "/etc/fonts", "--tmpfs", "/tmp", "--dir", "/work",
               "--proc", "/proc", "--dev", "/dev", "--ro-bind", worker, "/csl_media_worker.py",
               "--chdir", "/work", "--setenv", "HOME", "/work", "--setenv", "USER", "csl"]
    prefix = Path(python).parent.parent
    if (prefix / "pyvenv.cfg").is_file():
        command += ["--ro-bind", str(prefix), "/opt/csl-python"]
        executable = "/opt/csl-python/bin/" + Path(python).name
    else:
        executable = str(Path(python).resolve())
        if not executable.startswith("/usr/"):
            base = Path(sys.base_prefix).resolve()
            if Path(executable).parent.parent != base or base in (Path.home().resolve(), Path("/")):
                raise ValueError("Sandboxed media requires a system Python or virtualenv; configure CSL_MEDIA_PYTHON")
            command += ["--ro-bind", str(base), "/opt/csl-python"]
            executable = "/opt/csl-python/bin/" + Path(executable).name
    return [*command, executable, "-I", "/csl_media_worker.py"]


def decode(operation, data, mime, python):
    return _decode(operation, data, mime, python, os.environ.get("CSL_MEDIA_SANDBOX", "auto"))


@lru_cache(maxsize=4)
def _decode(operation, data, mime, python, mode):
    command = sandbox_command(python, mode)
    try:
        with tempfile.TemporaryDirectory(prefix="csl-media-") as temp:
            result = subprocess.run(command,
                input=json.dumps({"operation": operation, "data": data, "mime": mime}).encode(),
                capture_output=True, timeout=15, cwd=temp, env={"LANG": "C.UTF-8", "HOME": temp})
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Media decoding timed out; no partial conversion installed") from exc
    except OSError as exc:
        raise ValueError("Media decoder unavailable; run ./install-media.sh or set CSL_MEDIA_PYTHON") from exc
    if len(result.stdout) > MAX_PROJECTED_BYTES:
        raise ValueError("Media projection exceeds output limit")
    try:
        reply = json.loads(result.stdout)
    except ValueError as exc:
        if result.returncode and command[0] != python:
            raise ValueError("Media OS sandbox could not start; check bubblewrap support or explicitly use CSL_MEDIA_SANDBOX=process for trusted inputs") from exc
        raise ValueError("Invalid media decoder response") from exc
    if not isinstance(reply, dict):
        raise ValueError("Invalid media decoder response")
    if reply.get("missing_dependency"):
        if operation == "fixtures":
            raise ValueError("Synthetic media fixtures need media-test-requirements.txt in the optional media interpreter")
        raise ValueError("Optional media decoder dependencies missing; run ./install-media.sh or set CSL_MEDIA_PYTHON")
    if result.returncode or reply.get("error"):
        raise ValueError("Media is corrupt, protected, unsupported, or exceeds decoding limits")
    return reply


decode.cache_clear = _decode.cache_clear


def image(mime, data, digest=None):
    if mime not in SIGNATURES:
        raise ValueError("Unsupported image MIME")
    raw = raw_bytes(data, MAX_IMAGE_BYTES)
    if not raw.startswith(SIGNATURES[mime]) or (mime == "image/webp" and raw[8:12] != b"WEBP"):
        raise ValueError("Image signature/MIME mismatch")
    actual = hashlib.sha256(raw).hexdigest()
    if digest is not None and digest != actual:
        raise ValueError("Image digest mismatch")
    reply = decode("image", data, mime, interpreter())
    width, height = reply.get("width"), reply.get("height")
    if (reply.get("mime") != mime or type(width) is not int or type(height) is not int or
            not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 20_000_000):
        raise ValueError("Invalid media decoder image metadata")
    return {"type": "image", "mime_type": mime, "data": data, "sha256": actual}


def document(data, name="attachment.pdf", digest=None):
    raw = raw_bytes(data, MAX_PDF_BYTES)
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF signature")
    if not isinstance(name, str) or not name or len(name) > 255 or "\x00" in name:
        raise ValueError("Invalid PDF display name")
    actual = hashlib.sha256(raw).hexdigest()
    if digest is not None and digest != actual:
        raise ValueError("PDF digest mismatch")
    return {"type": "document", "mime_type": "application/pdf", "name": name, "data": data, "sha256": actual}


def project(block):
    checked = document(block["data"], block["name"], block["sha256"])
    reply = decode("pdf", checked["data"], "application/pdf", interpreter())
    pages = reply.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError("Invalid media decoder page count")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("text"), str) or len(page["text"]) > 200_000 or not isinstance(page.get("png"), str):
            raise ValueError("Invalid media decoder PDF page")
        raw = raw_bytes(page["png"], MAX_IMAGE_BYTES)
        if not raw.startswith(SIGNATURES["image/png"]):
            raise ValueError("Invalid media decoder PDF page image")
    return pages
