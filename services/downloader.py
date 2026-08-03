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
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Source cookies file — read-only golden copy, never written.
# Mount with `-v /root/wechat-x-youtube-data/cookies:/data/cookies` (no :ro;
# we need to read it but our code uses a copy so it's never modified).
COOKIES_SOURCE = "/data/cookies/youtube_cookies.txt"

# Working copy that yt-dlp may freely update during a session. Refreshed from
# COOKIES_SOURCE before each invocation so the source stays pristine even when
# YouTube returns a bot-check that would otherwise corrupt the cookie set.
COOKIES_RUNTIME = "/tmp/yt_dlp_cookies.txt"


def _stage_cookies() -> Optional[str]:
    """
    Copy the source cookies into a writable runtime path. Returns the
    runtime path if cookies are available, None otherwise.

    Called once per download. The copy is cheap (a few KB) and ensures
    that yt-dlp's automatic cookie-jar updates (or YouTube's hostile cookie
    rewrites on auth failure) don't touch the user-supplied source file.
    """
    if not os.path.exists(COOKIES_SOURCE):
        return None
    try:
        shutil.copy2(COOKIES_SOURCE, COOKIES_RUNTIME)
        return COOKIES_RUNTIME
    except Exception as exc:
        logger.warning(f"[downloader] Could not stage cookies: {exc}")
        return None


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


def _is_youtube_url(url: str) -> bool:
    """Best-effort hostname check — covers youtube.com, youtu.be, m.youtube.com, music.*"""
    lower = url.lower()
    return (
        "youtube.com/" in lower
        or "youtu.be/" in lower
        or lower.startswith("https://youtu.be")
        or lower.startswith("http://youtu.be")
    )


def download_url(url: str, audio_only: bool = False, force_ytdlp: bool = False) -> DownloadResult:
    """
    Download a single URL.

    Routing:
        YouTube → cobalt API (yt-dlp is IP-blocked on data-center hosts)
        Anything else → yt-dlp (the well-trodden path)

    Args:
        url: The media URL (YouTube, X, TikTok, Bilibili, etc.).
        audio_only: If True, extract audio-only mp3. Otherwise video capped
                    at settings.DOWNLOAD_MAX_HEIGHT.
        force_ytdlp: Skip the cobalt routing even for YouTube. Used as a
                    second attempt when cobalt fails (the proxy + tv/android
                    player clients sometimes succeed where cobalt doesn't).

    Returns:
        DownloadResult — never raises; failures are reported via the .error field.
    """
    # ----- YouTube goes through cobalt ----------------------------------
    if _is_youtube_url(url) and not force_ytdlp:
        from services.cobalt_client import download_via_cobalt
        logger.info(f"[downloader] YouTube detected — routing to cobalt: {url}")
        cobalt = download_via_cobalt(url, audio_only=audio_only)
        return DownloadResult(
            success=cobalt.success,
            file_path=cobalt.file_path,
            file_size_mb=cobalt.file_size_mb,
            duration=cobalt.duration,
            title=cobalt.title,
            error=cobalt.error,
        )

    # ----- Everything else: yt-dlp --------------------------------------
    output_dir = Path(settings.VIDEO_OUTPUT_DIR)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return DownloadResult(success=False, error=f"Cannot create output dir: {exc}")

    # Output template — keep readable for SFTP browsing, include video ID for uniqueness.
    # IMPORTANT: use .80B (BYTES) not .100s (chars) — Linux filename limit is 255 bytes,
    # and Chinese/Japanese/Korean titles use 3 bytes per char. A 100-char Chinese title
    # would be ~300 bytes and overflow when yt-dlp adds intermediate suffixes like
    # ".fhls-510.mp4.part-Frag494.part" (35 extra bytes).
    # 80 bytes ≈ 26 CJK chars or 80 ASCII chars — readable AND safely under limit.
    output_template = str(output_dir / "%(title).80B [%(id)s].%(ext)s")

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
    # We pass a fresh-copied runtime path (not the source) so yt-dlp's writes
    # don't corrupt the golden cookies file when YouTube hostile-rewrites them.
    cookies_path = _stage_cookies()
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
        logger.debug(f"[downloader] Using staged cookies: {cookies_path}")
    else:
        logger.debug(f"[downloader] No cookies file at {COOKIES_SOURCE} — proceeding without auth")

    # Residential proxy — only if configured. Lets YouTube see a non-datacenter IP.
    if settings.PROXY_URL:
        cmd.extend(["--proxy", settings.PROXY_URL])
        logger.debug(f"[downloader] Using proxy: {settings.PROXY_URL.split('@')[-1]}")

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
