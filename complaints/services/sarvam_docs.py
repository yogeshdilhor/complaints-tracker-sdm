"""Sarvam's Document Intelligence ("Digitise"): page images in, clean Hindi/English text out.

It reads handwriting, stamps and signature blocks far better than the local scan reader.
The pages themselves are sent to Sarvam, unmasked. Best effort: None on any failure.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from django.conf import settings

logger = logging.getLogger(__name__)

BASE = "https://api.sarvam.ai/doc-ai/v1/job"
POLL_SECONDS = 2
MAX_WAIT_SECONDS = 90


def is_configured() -> bool:
    return bool(settings.SARVAM_API_KEY)


def _multipart(fields: dict[str, str], filename: str, content: bytes, mime: str):
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _call(url: str, data: bytes | None = None, content_type: str | None = None):
    headers = {"api-subscription-key": settings.SARVAM_API_KEY}
    if content_type:
        headers["content-type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - fixed https
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.load(response)


def digitise_pages(content: bytes, mime_type: str, language: str = "hi-IN") -> list[str] | None:
    """The text of each page of a PDF (up to 10 pages) or image, read from the page pictures."""
    if not is_configured():
        return None
    extension = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}.get(mime_type)
    if extension is None:
        return None
    try:
        body, content_type = _multipart(
            {"language": language, "output_format": "md"},
            f"letter.{extension}",
            content,
            mime_type,
        )
        job = _call(f"{BASE}/digitise", body, content_type)
        job_id = job["job_id"]
        logger.info("Sarvam digitise job %s started", job_id)
        waited = 0
        while True:
            status = _call(f"{BASE}/{job_id}/status")["status"]
            if status not in ("pending", "running"):
                break
            if waited >= MAX_WAIT_SECONDS:
                logger.warning("Sarvam digitise job %s timed out", job_id)
                return None
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        if status not in ("completed", "partially_completed"):
            logger.warning("Sarvam digitise job %s ended as %s", job_id, status)
            return None
        url = _call(f"{BASE}/{job_id}/download-url")["url"]
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - Sarvam's URL
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        pages = []
        for name in sorted(n for n in archive.namelist() if re.search(r"page_\d+\.json$", n)):
            blocks = json.loads(archive.read(name)).get("blocks", [])
            blocks.sort(key=lambda b: b.get("reading_order", 0))
            pages.append("\n".join(b.get("text", "") for b in blocks if b.get("text")).strip())
    except urllib.error.HTTPError as error:
        logger.warning("Sarvam digitise failed: HTTP %s %s", error.code, error.read()[:300])
        return None
    except Exception:  # network, zip or shape errors: never block the page
        logger.warning("Sarvam digitise failed", exc_info=True)
        return None
    return pages if any(pages) else None


def digitise(content: bytes, mime_type: str, language: str = "hi-IN") -> str | None:
    """The whole text of a PDF or image, pages joined."""
    pages = digitise_pages(content, mime_type, language)
    return "\n\n".join(p for p in pages if p) if pages else None
