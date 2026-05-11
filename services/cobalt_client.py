"""
Cobalt API client — for YouTube downloads where yt-dlp gets IP-blocked.

Why this exists:
  YouTube increasingly blocks data-center IPs (Hetzner, AWS, etc.) from yt-dlp's
  extraction even with valid auth cookies. Cobalt (https://github.com/imputnet/cobalt)
  is a community-maintained open-source media downloader that uses different
  extraction methods (mobile-app endpoints, embed APIs) and sometimes succeeds
  where yt-dlp fails.

Deployment:
  Cobalt runs as a separate Docker container on the same network as our app:

      docker run -d \\
        --name cobalt-api \\
        --restart=always \\
        --network n8n_default \\
        -e API_URL="http://cobalt-api:9000/" \\
        -e DURATION_LIMIT=10800 \\
        ghcr.io/imputnet/cobalt:10

  Our app reaches it via the internal Docker DNS name `cobalt-api:9000`.

Routing strategy (see services/downloader.py):
  YouTube URL  → cobalt first, no yt-dlp fallback (it's known to fail too)
  Anything else → yt-dlp (works without cobalt)
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Internal cobalt API URL. Override with COBALT_API_URL env var if needed.
COBALT_API_URL = os.getenv("COBALT_API_URL", "http://cobalt-api:9000/")

# Timeout for the API call itself (cobalt responds fast — just metadata).
# Actual download is a separate streaming GET with a longer timeout.
COBALT_API_TIMEOUT = 30
COBALT_DOWNLOAD_TIMEOUT = 600


@dataclass
class CobaltDownloadResult:
    """Mirrors the shape of services.downloader.DownloadResult so callers can swap freely."""
    success: bool
    file_path: Optional[str] = None
    file_size_mb: Optional[float] = None
    duration: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None


def _ask_cobalt(url: str, audio_only: bool) -> dict:
    """
    Ask cobalt to prepare a download for the given media URL.

    Returns the raw JSON response, or {"status": "error", ...} on transport failure.
    Cobalt API spec: https://github.com/imputnet/cobalt/blob/main/docs/api.md
    """
    payload = {
        "url": url,
        "downloadMode": "audio" if audio_only else "auto",
    }
    if not audio_only:
        payload["videoQuality"] = str(settings.DOWNLOAD_MAX_HEIGHT)

    try:
        resp = requests.post(
            COBALT_API_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=COBALT_API_TIMEOUT,
        )
        # Cobalt returns 200 with status="error" for downloader-level failures,
        # but non-200 means infrastructure problem.
        if not resp.ok:
            return {"status": "error", "error": {"text": f"HTTP {resp.status_code}: {resp.text[:300]}"}}
        return resp.json()
    except requests.RequestException as exc:
        return {"status": "error", "error": {"text": f"cobalt unreachable: {exc}"}}


def _sanitize_filename(name: str, max_bytes: int = 200) -> str:
    """Strip filesystem-illegal characters and clamp to a Linux-safe byte length."""
    cleaned = "".join(c if c not in '\\/<>:"|?*\n\r\t\0' else "_" for c in name).strip()
    cleaned = cleaned or "cobalt_download"

    # Byte-aware truncation (Chinese chars are 3 bytes each in UTF-8)
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned

    # Preserve extension if present
    p = Path(cleaned)
    ext = p.suffix
    stem = p.stem
    # Try shrinking stem in 10-char steps until it fits
    while stem and len((stem + ext).encode("utf-8")) > max_bytes:
        stem = stem[:-10]
    return stem + ext


def download_via_cobalt(url: str, audio_only: bool = False) -> CobaltDownloadResult:
    """
    Full cobalt download flow:
      1. Call cobalt API to get a download URL (tunnel or redirect)
      2. Stream the bytes to disk in settings.VIDEO_OUTPUT_DIR
      3. Return a DownloadResult-shaped object

    Never raises — failures come back via .success=False and .error.
    """
    output_dir = Path(settings.VIDEO_OUTPUT_DIR)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return CobaltDownloadResult(success=False, error=f"Cannot create output dir: {exc}")

    logger.info(f"[cobalt] Requesting: {url} (audio_only={audio_only})")
    data = _ask_cobalt(url, audio_only)

    status = data.get("status")

    # --- Resolve download URL from response -----------------------------
    download_url: Optional[str] = None
    filename: Optional[str] = data.get("filename")

    if status in ("tunnel", "redirect"):
        download_url = data.get("url")
    elif status == "picker":
        items = data.get("picker") or []
        if items:
            download_url = items[0].get("url")
            filename = filename or items[0].get("filename")
    elif status == "error":
        err = data.get("error", {})
        msg = f"cobalt error [{err.get('code', '?')}]: {err.get('text', err)}"
        logger.error(f"[cobalt] {msg}")
        return CobaltDownloadResult(success=False, error=msg)
    else:
        msg = f"unexpected cobalt status: {status} — body: {str(data)[:200]}"
        logger.error(f"[cobalt] {msg}")
        return CobaltDownloadResult(success=False, error=msg)

    if not download_url:
        return CobaltDownloadResult(success=False, error="cobalt returned no download URL")

    # --- Stream the file to disk ----------------------------------------
    safe_filename = _sanitize_filename(filename or "cobalt_download.mp4")
    output_path = output_dir / safe_filename

    logger.info(f"[cobalt] Fetching tunnel → {output_path}")
    try:
        with requests.get(download_url, stream=True, timeout=COBALT_DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
    except Exception as exc:
        # Clean up partial file
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return CobaltDownloadResult(success=False, error=f"tunnel download failed: {exc}")

    size_mb = round(output_path.stat().st_size / (1024 * 1024), 1)
    logger.info(f"[cobalt] Done: {output_path} ({size_mb} MB)")

    return CobaltDownloadResult(
        success=True,
        file_path=str(output_path),
        file_size_mb=size_mb,
        title=safe_filename,
    )
