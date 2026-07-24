"""
YouTube transcript fetcher — Phase 4.

Fetches the full transcript of a YouTube video via youtube-transcript-api,
routed through the residential proxy (PROXY_URL) because YouTube blocks
transcript requests from data-center IPs (verified 2026-07: Hetzner direct
= IpBlocked; through DataImpulse residential proxy = works first try).

Retries up to 3 times on block errors — the proxy pool rotates exit IPs per
request, so a retry usually lands on a clean IP even if one exit is flagged.
"""

import re
from dataclasses import dataclass
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Language preference for transcripts. find_transcript() walks this list and
# prefers manually-created transcripts over auto-generated ones per language.
PREFERRED_LANGUAGES = ["en", "zh-Hans", "zh-Hant", "zh", "zh-CN", "zh-TW"]

MAX_FETCH_ATTEMPTS = 5

_VIDEO_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
    r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/live/)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
]


@dataclass
class TranscriptResult:
    success: bool
    video_id: Optional[str] = None
    title: Optional[str] = None          # from oEmbed; None if unavailable
    channel: Optional[str] = None        # from oEmbed; None if unavailable
    language: Optional[str] = None
    duration_seconds: int = 0
    text: str = ""                       # full transcript, plain text
    error: Optional[str] = None


def extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-char video ID out of any common YouTube URL shape."""
    for pattern in _VIDEO_ID_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _proxies() -> Optional[dict]:
    """requests-style proxies dict from PROXY_URL, or None when unset."""
    if not settings.PROXY_URL:
        return None
    return {"http": settings.PROXY_URL, "https": settings.PROXY_URL}


def _fetch_oembed_metadata(url: str) -> dict:
    """
    Best-effort title/channel lookup via YouTube's public oEmbed endpoint.
    Routed through the proxy like everything else. Failure is non-fatal —
    the analyzer infers a title from the transcript when this comes back empty.
    """
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            proxies=_proxies(),
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            return {"title": data.get("title"), "channel": data.get("author_name")}
    except Exception as exc:
        logger.debug(f"[transcript] oEmbed lookup failed (non-fatal): {exc}")
    return {}


def _format_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fetch_transcript(url: str) -> TranscriptResult:
    """
    Fetch the full transcript for a YouTube URL.

    Never raises — failures are reported via .success=False / .error.
    """
    video_id = extract_video_id(url)
    if not video_id:
        return TranscriptResult(success=False, error=f"Could not extract video ID from: {url}")

    # Imported lazily so the app still boots if the package is missing
    # (e.g. SCF deployment, which doesn't run Phase 4).
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig
    except ImportError:
        return TranscriptResult(success=False, error="youtube-transcript-api not installed")

    proxy_config = None
    if settings.PROXY_URL:
        proxy_config = GenericProxyConfig(
            http_url=settings.PROXY_URL,
            https_url=settings.PROXY_URL,
        )
    else:
        logger.warning("[transcript] PROXY_URL not set — direct fetch will fail on data-center IPs")

    last_error = "unknown"
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            ytt = YouTubeTranscriptApi(proxy_config=proxy_config)
            transcript_list = ytt.list(video_id)

            # Prefer our language list (manual > generated per language);
            # fall back to whatever transcript exists.
            try:
                transcript = transcript_list.find_transcript(PREFERRED_LANGUAGES)
            except Exception:
                transcript = next(iter(transcript_list))

            fetched = transcript.fetch()
            snippets = list(fetched)
            if not snippets:
                return TranscriptResult(success=False, video_id=video_id, error="Transcript is empty")

            text = "\n".join(s.text.strip() for s in snippets if s.text.strip())
            last = snippets[-1]
            duration = int(last.start + (last.duration or 0))

            meta = _fetch_oembed_metadata(url)

            logger.info(
                f"[transcript] {video_id}: {len(snippets)} snippets, "
                f"{len(text)} chars, lang={transcript.language_code}, attempt {attempt}"
            )
            return TranscriptResult(
                success=True,
                video_id=video_id,
                title=meta.get("title"),
                channel=meta.get("channel"),
                language=transcript.language_code,
                duration_seconds=duration,
                text=text,
            )

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            exc_name = type(exc).__name__
            # Retry EVERYTHING, including TranscriptsDisabled / NoTranscriptFound.
            # Known youtube-transcript-api quirk: when YouTube blocks a request,
            # it can serve a page the library misreads as "transcripts disabled",
            # so those errors are NOT reliable on a single attempt. The rotating
            # proxy pool hands out a fresh exit IP each try; genuinely disabled
            # videos just cost a few extra cheap requests before failing for real.
            logger.warning(f"[transcript] {video_id} attempt {attempt}/{MAX_FETCH_ATTEMPTS} failed: {exc_name}")

    return TranscriptResult(success=False, video_id=video_id, error=last_error[:500])


def format_duration(result: TranscriptResult) -> str:
    """Human-readable duration string for Notion (e.g. '12:34' or '1:02:03')."""
    return _format_duration(result.duration_seconds)
