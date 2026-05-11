"""
Media downloader service — wraps yt-dlp.

Used by:
  - Telegram /dl command (routes/telegram.py) — fast path
  - Notion Status poller (services/poller.py) — batch/deferred path

Design notes:
  - We shell out to yt-dlp rather than use yt_dlp Python API directly because:
    1. The CLI surface is more stable across yt-dlp updates than the Python API
    2. Easier to add timeout, isolate per-call state
    3. Errors are easier to capture as plain text
  - yt-dlp itself handles platform-specific quirks (DASH stream merging, X video
    extraction, etc.). ffmpeg is required for the merging — installed via Dockerfile.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Optional cookies file — if present, yt-dlp uses it for auth.
# Bypasses YouTube's "Sign in to confirm you're not a bot" check on data-center IPs.
# To enable: export cookies from your browser via "Get cookies.txt LOCALLY"
# extension and place at the host path /root/wechat-x-youtube-data/cookies/
# (mounted into the container at /data/cookies/).
COOKIES_FILE = "/data/cookies/youtube_cookies.txt"


@dataclass
class DownloadResult:
    """Result of a single yt-dlp invocation."""
    success: bool
    file_path: Optional[str] = None
    file_size_mb: Optional[float] = None
    duration: Optional[str] = None  # human-readable like "12:34"
    title: Optional[str] = None
    error: Optional[str] = None


def _build_format_args(audio_only: bool, max_height: str) -> List[str]:
    """yt-dlp format selector for our two modes."""
    if audio_only:
        # Extract best audio, convert to mp3 at best quality
        return [
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
        ]
    # Video: cap at max_height, prefer mp4 container, fall back to anything that fits
    return [
        "-f",
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={max_height}]+bestaudio"
        f"/best[height<={max_height}]"
        f"/best",
        "--merge-output-format", "mp4",
    ]


def download_url(url: str, audio_only: bool = False) -> DownloadResult:
    """
    Download a single URL via yt-dlp.

    Args:
        url: The media URL (YouTube, X, TikTok, Bilibili, etc.).
        audio_only: If True, extract audio-only mp3. Otherwise video capped
                    at settings.DOWNLOAD_MAX_HEIGHT.

    Returns:
        DownloadResult — never raises; failures are reported via the .error field.
    """
    output_dir = Path(settings.VIDEO_OUTPUT_DIR)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return DownloadResult(success=False, error=f"Cannot create output dir: {exc}")

    # Output template — keep readable for SFTP browsing, include video ID for uniqueness.
    # %(title).100s truncates the title to 100 chars; [%(id)s] makes it dedup-safe.
    output_template = str(output_dir / "%(title).100s [%(id)s].%(ext)s")

    cmd = [
        "yt-dlp",
        *_build_format_args(audio_only, settings.DOWNLOAD_MAX_HEIGHT),
        "-o", output_template,
        "--no-playlist",        # If URL is a playlist, only grab the linked video
        "--no-warnings",
        "--no-progress",
        # YouTube bot-check workaround: try TV / Android / iOS clients first.
        # YouTube blocks data-center IPs from the default "web" player but is
        # more lenient on TV / mobile clients. yt-dlp ignores this arg for
        # non-YouTube URLs (Twitter, TikTok, Bilibili, etc.).
        "--extractor-args", "youtube:player_client=tv,android,ios,web",
        # --print emits these AFTER the file is moved to its final location
        "--print", "after_move:filepath",
        "--print", "after_move:%(title)s",
        "--print", "after_move:%(duration_string)s",
    ]

    # Add browser cookies if the user mounted a cookies file.
    # YouTube on data-center IPs (Hetzner, AWS) requires logged-in cookies.
    if os.path.exists(COOKIES_FILE):
        cmd.extend(["--cookies", COOKIES_FILE])
        logger.debug(f"[downloader] Using cookies file: {COOKIES_FILE}")
    else:
        logger.debug(f"[downloader] No cookies file at {COOKIES_FILE} — proceeding without auth")

    cmd.append(url)

    logger.info(f"[downloader] yt-dlp start: {url} (audio_only={audio_only})")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.DOWNLOAD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"[downloader] Timeout after {settings.DOWNLOAD_TIMEOUT_SECONDS}s: {url}")
        return DownloadResult(
            success=False,
            error=f"Download timed out after {settings.DOWNLOAD_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        logger.error("[downloader] yt-dlp binary not found in container")
        return DownloadResult(success=False, error="yt-dlp not installed")
    except Exception as exc:
        logger.error(f"[downloader] Unexpected error: {exc}", exc_info=True)
        return DownloadResult(success=False, error=str(exc)[:500])

    if result.returncode != 0:
        # yt-dlp exit codes: 0=success, 1=generic error, 2=user-cancelled, 100=update needed
        # Last few lines of stderr are usually the most informative
        err_tail = "\n".join(result.stderr.strip().split("\n")[-5:])
        logger.error(f"[downloader] yt-dlp failed (exit {result.returncode}): {err_tail}")
        return DownloadResult(success=False, error=err_tail[:500])

    # Parse our three --print lines (last 3 non-empty lines of stdout)
    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    if len(lines) < 1:
        return DownloadResult(success=False, error="yt-dlp succeeded but produced no output")

    # Take the last 3 lines as filepath / title / duration (in that order)
    filepath = lines[-3] if len(lines) >= 3 else lines[-1]
    title = lines[-2] if len(lines) >= 3 else None
    duration = lines[-1] if len(lines) >= 3 else None

    if not os.path.exists(filepath):
        return DownloadResult(
            success=False,
            error=f"yt-dlp claimed success but file missing: {filepath}",
        )

    size_mb = round(os.path.getsize(filepath) / (1024 * 1024), 1)

    logger.info(f"[downloader] Done: {filepath} ({size_mb} MB)")

    return DownloadResult(
        success=True,
        file_path=filepath,
        file_size_mb=size_mb,
        duration=duration if duration and duration != "NA" else None,
        title=title,
    )
