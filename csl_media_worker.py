"""Optional decoder process: stdin bytes only; no credentials, links or source paths."""

import base64
from contextlib import closing
import io
import json
import math
import re
import resource
import sys
import warnings


def process(request):
    from PIL import Image
    if request["operation"] == "fixtures":
        return fixtures(json.loads(request["data"]))
    raw = base64.b64decode(request["data"], validate=True)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Asset byte limit")
    if request["operation"] == "image":
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                mime = Image.MIME.get(image.format)
                if mime != request["mime"] or max(image.size) > 8192 or image.width * image.height > 20_000_000 or getattr(image, "is_animated", False):
                    raise ValueError("Image MIME, dimensions or animation unsupported")
                reply = {"mime": mime, "width": image.width, "height": image.height}
                image.verify()
            # Force actual decoding too: verify alone does not decode JPEG pixel data.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        return reply
    if request["operation"] != "pdf":
        raise ValueError("Unknown decoder operation")
    import pypdfium2 as pdfium
    pages = []
    with closing(pdfium.PdfDocument(raw)) as document:
        if not 1 <= len(document) <= 20:
            raise ValueError("PDF page limit")
        for index in range(len(document)):
            with closing(document[index]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    raise ValueError("Invalid PDF dimensions")
                with closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_range()
                    if len(text) > 200_000:
                        raise ValueError("PDF text limit")
                scale = min(96 / 72, 1600 / max(width, height))
                with closing(page.render(scale=scale)) as bitmap:
                    stream = io.BytesIO()
                    bitmap.to_pil().save(stream, format="PNG")
                    pages.append({"text": text, "png": base64.b64encode(stream.getvalue()).decode()})
    return {"pages": pages}


def fixtures(markers):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    if set(markers) != {"image", "receipt", "page", "scan"} or any(not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9-]{1,18}", value) for value in markers.values()):
        raise ValueError("Invalid synthetic fixture markers")
    image = Image.new("RGB", (600, 400), (230, 20, 30))
    ImageDraw.Draw(image).text((25, 150), markers["image"], font=ImageFont.load_default(size=65), fill="white")
    png = io.BytesIO()
    image.save(png, format="PNG")
    invoice = io.BytesIO()
    pdf = canvas.Canvas(invoice, pagesize=(600, 400), invariant=1)
    pdf.setFont("Helvetica", 22)
    pdf.drawString(40, 300, "Invoice receipt: " + markers["receipt"])
    pdf.drawString(40, 250, "Invoice amount: 273.40")
    pdf.showPage()
    pdf.setFont("Helvetica", 22)
    pdf.drawString(40, 300, "Second-page code: " + markers["page"])
    pdf.save()
    scan = Image.new("RGB", (600, 400), (10, 35, 230))
    ImageDraw.Draw(scan).text((25, 150), markers["scan"], font=ImageFont.load_default(size=65), fill="white")
    scanned = io.BytesIO()
    pdf = canvas.Canvas(scanned, pagesize=(600, 400), invariant=1)
    pdf.drawImage(ImageReader(scan), 0, 0, width=600, height=400)
    pdf.save()
    return {"image": base64.b64encode(png.getvalue()).decode(),
            "pdf": base64.b64encode(invoice.getvalue()).decode(),
            "scan": base64.b64encode(scanned.getvalue()).decode()}


def main():
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        raw = sys.stdin.buffer.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("Input limit")
        reply = process(json.loads(raw))
        output = json.dumps(reply)
        if len(output) > 32 * 1024 * 1024:
            raise ValueError("Output limit")
        print(output)
    except ImportError:
        print(json.dumps({"missing_dependency": True}))
        return 2
    except Exception:
        print(json.dumps({"error": "Invalid or unsafe media"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
