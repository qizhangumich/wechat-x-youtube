"""
Audio-transcription fallback — Phase 4.5.

When YouTube captions can't be fetched (TranscriptsDisabled, or persistent
IP blocks that survive all proxy retries), we fall back to transcribing the
actual audio:

    download audio (cobalt for YouTube, yt-dlp otherwise — services/downloader)
        │
        ▼
    ffmpeg → 32 kbps mono mp3 (predictable size: ~0.24 MB/min)
        │
        ▼
    split into ≤20-min chunks (OpenAI audio API: 25 MB / ~25 min per request)
        │
        ▼
    POST /v1/audio/transcriptions per chunk (raw multipart via requests —
    the openai SDK needs pydantic 2.x, this project pins 1.8.2)
        │
        ▼
    joined plain-text transcript

Cost: gpt-4o-mini-transcribe ≈ $0.003/min ($0.18/hour of video).
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"

# Chunk length in seconds. gpt-4o-(mini-)transcribe rejects long inputs and
# the API caps uploads at 25 MB; 20-min chunks at 32 kbps are ~4.8 MB — safe
# on both axes for every supported model (incl. whisper-1).
CHUNK_SECONDS = 1200

# Per-chunk upload+transcription timeout.
CHUNK_TIMEOUT_SECONDS = 600

FFMPEG_TIMEOUT_SECONDS = 900


@dataclass
class AudioTranscriptionResult:
    success: bool
    text: str = ""
    duration_seconds: int = 0
    error: Optional[str] = None


def _probe_duration_seconds(path: str) -> int:
    """Duration via ffprobe; 0 if it can't be determined (non-fatal)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return int(float(out.stdout.strip()))
    except Exception:
        return 0


def _compress_to_mp3(src: str, dst: str) -> Optional[str]:
    """Re-encode any audio container to 32 kbps mono mp3. Returns error or None."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-b:a", "32k", dst],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"ffmpeg re-encode timed out after {FFMPEG_TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        return "ffmpeg not installed"
    if result.returncode != 0:
        return "ffmpeg failed: " + "\n".join(result.stderr.strip().split("\n")[-3:])[:300]
    return None


def _split_chunks(mp3_path: str, chunk_dir: str) -> List[str]:
    """
    Split the mp3 into CHUNK_SECONDS segments (stream copy — fast, no
    re-encode). Returns the ordered chunk paths; falls back to the whole
    file if splitting fails.
    """
    pattern = os.path.join(chunk_dir, "chunk_%03d.mp3")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-f", "segment",
             "-segment_time", str(CHUNK_SECONDS), "-c", "copy", pattern],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            chunks = sorted(Path(chunk_dir).glob("chunk_*.mp3"))
            if chunks:
                return [str(p) for p in chunks]
    except Exception as exc:
        logger.warning(f"[audio-transcribe] Chunk split failed ({exc}) — sending whole file")
    return [mp3_path]


def _transcribe_file(path: str) -> str:
    """
    Transcribe one audio file via the OpenAI API. Raises RuntimeError on
    API failure (caller aggregates into the result object).
    """
    with open(path, "rb") as fh:
        resp = requests.post(
            OPENAI_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            data={
                "model": settings.OPENAI_TRANSCRIBE_MODEL,
                "response_format": "text",
            },
            files={"file": (os.path.basename(path), fh, "audio/mpeg")},
            timeout=CHUNK_TIMEOUT_SECONDS,
        )
    if not resp.ok:
        raise RuntimeError(f"OpenAI transcription {resp.status_code}: {resp.text[:300]}")
    return resp.text.strip()


def transcribe_url_audio(url: str) -> AudioTranscriptionResult:
    """
    Full fallback pipeline for one media URL. Never raises.

    The downloaded source file is deleted afterwards — it exists only to be
    transcribed, unlike /dl downloads which are kept on purpose.
    """
    if not settings.OPENAI_API_KEY:
        return AudioTranscriptionResult(success=False, error="OPENAI_API_KEY not set")

    # Imported here (not module top) to keep this module importable in
    # environments without the downloader's dependencies.
    from services.downloader import download_url

    logger.info(f"[audio-transcribe] Downloading audio for {url}")
    dl = download_url(url, audio_only=True)
    if not dl.success or not dl.file_path:
        return AudioTranscriptionResult(
            success=False, error=f"audio download failed: {dl.error}"
        )

    tmp_dir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        mp3_path = os.path.join(tmp_dir, "audio.mp3")
        err = _compress_to_mp3(dl.file_path, mp3_path)
        if err:
            return AudioTranscriptionResult(success=False, error=err)

        duration = _probe_duration_seconds(mp3_path)
        size_mb = round(os.path.getsize(mp3_path) / (1024 * 1024), 1)
        chunks = _split_chunks(mp3_path, tmp_dir) if duration > CHUNK_SECONDS else [mp3_path]
        logger.info(
            f"[audio-transcribe] {size_mb} MB / {duration}s of audio → "
            f"{len(chunks)} chunk(s) → {settings.OPENAI_TRANSCRIBE_MODEL}"
        )

        texts: List[str] = []
        for i, chunk in enumerate(chunks, 1):
            try:
                texts.append(_transcribe_file(chunk))
                logger.info(f"[audio-transcribe] Chunk {i}/{len(chunks)} done")
            except Exception as exc:
                # A missing middle chunk would silently corrupt the transcript
                # for analysis — fail the whole fallback instead.
                return AudioTranscriptionResult(
                    success=False,
                    error=f"chunk {i}/{len(chunks)} failed: {str(exc)[:300]}",
                )

        text = "\n".join(t for t in texts if t)
        if not text:
            return AudioTranscriptionResult(success=False, error="transcription returned empty text")

        return AudioTranscriptionResult(success=True, text=text, duration_seconds=duration)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Remove the source audio download — created solely for transcription.
        try:
            os.remove(dl.file_path)
        except OSError:
            pass
