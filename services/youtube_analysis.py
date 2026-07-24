"""
YouTube analysis pipeline — Phase 4 orchestration + Notion writer.

Flow (runs as a FastAPI background task after a YouTube link is saved):

    fetch_transcript(url)          services/transcript_service.py (proxied)
        │
        ▼
    analyze_transcript(text)       services/analyzer.py (Claude, structured output)
        │
        ▼
    _save_analysis_to_notion()     writes to the SEPARATE youtube-analysis DB:
        • structured properties (Summary, Key Points, Companies, ...)
        • full transcript in the page body (chunked into paragraph blocks)

Dedup: if a page with the same Video URL already exists in the analysis DB,
the pipeline skips — re-sharing a video doesn't re-analyze (and re-bill).
"""

from datetime import datetime, timezone
from typing import List, Optional

import requests

from config import settings
from services.analyzer import AnalysisResult, analyze_transcript
from services.transcript_service import TranscriptResult, fetch_transcript, format_duration
from utils.logger import get_logger

logger = get_logger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Property names on the youtube-analysis database — case + space sensitive.
PROP_TITLE       = "Title"
PROP_VIDEO_URL   = "Video URL"
PROP_SUMMARY     = "Summary"
PROP_KEY_POINTS  = "Key Points"
PROP_COMPANIES   = "Companies"
PROP_TECH        = "Technologies"
PROP_PEOPLE      = "People"
PROP_TOPICS      = "Topics"
PROP_DURATION    = "Duration"
PROP_ANALYZED_AT = "Analyzed At"

# Notion limits
RICH_TEXT_LIMIT = 2000        # chars per rich_text content
BLOCKS_PER_REQUEST = 90       # API cap is 100; leave margin
MULTISELECT_MAX_ITEMS = 10    # keep tag columns tidy


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:RICH_TEXT_LIMIT]}}] if value else []


def _multi_select(items: List[str]) -> list:
    """Notion multi-select option names cannot contain commas — sanitize."""
    out = []
    for item in items[:MULTISELECT_MAX_ITEMS]:
        name = item.replace(",", " /").strip()[:100]
        if name:
            out.append({"name": name})
    return out


def _already_analyzed(url: str) -> bool:
    """True if a page with this Video URL already exists in the analysis DB."""
    try:
        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{settings.NOTION_YOUTUBE_DB_ID}/query",
            headers=_headers(),
            json={
                "filter": {"property": PROP_VIDEO_URL, "url": {"equals": url}},
                "page_size": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return len(resp.json().get("results", [])) > 0
    except requests.RequestException as exc:
        logger.warning(f"[yt-analysis] Dedup check failed (proceeding anyway): {exc}")
        return False


def _argument_blocks(analysis: AnalysisResult) -> List[dict]:
    """
    Render the argument-mining result as Notion blocks:

        ## Arguments & Logic
        ### <claim>
        • <evidence item>            (bulleted list)
        Logic: <reasoning>           (italic paragraph)
    """
    if not analysis.arguments:
        return []

    blocks: List[dict] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "Arguments & Logic"}}]},
        }
    ]
    for arg in analysis.arguments:
        claim = str(arg.get("claim", "")).strip()
        if not claim:
            continue
        blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"text": {"content": claim[:RICH_TEXT_LIMIT]}}]},
        })
        for ev in arg.get("evidence", []) or []:
            ev = str(ev).strip()
            if ev:
                blocks.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"text": {"content": ev[:RICH_TEXT_LIMIT]}}]},
                })
        reasoning = str(arg.get("reasoning", "")).strip()
        if reasoning:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "text": {"content": f"Logic: {reasoning}"[:RICH_TEXT_LIMIT]},
                        "annotations": {"italic": True},
                    }]
                },
            })
    return blocks


def _transcript_blocks(transcript_text: str) -> List[dict]:
    """Chunk the transcript into Notion paragraph blocks (≤2000 chars each)."""
    blocks: List[dict] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "Transcript"}}]},
        }
    ]
    lines = transcript_text.split("\n")
    chunk = ""
    for line in lines:
        # +1 for the newline we re-add
        if len(chunk) + len(line) + 1 > RICH_TEXT_LIMIT:
            if chunk:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
                })
            # A single line longer than the limit gets hard-split
            while len(line) > RICH_TEXT_LIMIT:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": line[:RICH_TEXT_LIMIT]}}]},
                })
                line = line[RICH_TEXT_LIMIT:]
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
        })
    return blocks


def _save_analysis_to_notion(
    url: str,
    transcript: TranscriptResult,
    analysis: AnalysisResult,
) -> Optional[str]:
    """
    Create the analysis page. Returns the page_id, or None on failure.
    Transcript goes into the page body; the first BLOCKS_PER_REQUEST blocks
    ride along on page creation, the rest are appended in batches.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Key Points property = compact list of the claims (full detail with
    # evidence + reasoning lives in the page body's Arguments & Logic section)
    key_points_text = "\n".join(
        f"• {a.get('claim', '')}" for a in analysis.arguments if a.get("claim")
    )
    people_text = ", ".join(analysis.people)
    title = analysis.title[:200] or "Untitled video"
    if transcript.channel and transcript.channel not in title:
        title = f"{title} — {transcript.channel}"

    properties = {
        PROP_TITLE:       {"title": [{"text": {"content": title[:255]}}]},
        PROP_VIDEO_URL:   {"url": url},
        PROP_SUMMARY:     {"rich_text": _rich_text(analysis.summary)},
        PROP_KEY_POINTS:  {"rich_text": _rich_text(key_points_text)},
        PROP_COMPANIES:   {"multi_select": _multi_select(analysis.companies)},
        PROP_TECH:        {"multi_select": _multi_select(analysis.technologies)},
        PROP_PEOPLE:      {"rich_text": _rich_text(people_text)},
        PROP_TOPICS:      {"multi_select": _multi_select(analysis.topics)},
        PROP_DURATION:    {"rich_text": _rich_text(format_duration(transcript))},
        PROP_ANALYZED_AT: {"date": {"start": now_iso}},
    }

    all_blocks = _argument_blocks(analysis) + _transcript_blocks(transcript.text)
    first_batch = all_blocks[:BLOCKS_PER_REQUEST]
    remaining = all_blocks[BLOCKS_PER_REQUEST:]

    resp = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(),
        json={
            "parent": {"database_id": settings.NOTION_YOUTUBE_DB_ID},
            "properties": properties,
            "children": first_batch,
        },
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"[yt-analysis] Page create failed {resp.status_code}: {resp.text[:600]}")
        return None
    page_id = resp.json().get("id", "")

    # Append the rest of the transcript in batches
    while remaining:
        batch, remaining = remaining[:BLOCKS_PER_REQUEST], remaining[BLOCKS_PER_REQUEST:]
        append = requests.patch(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=_headers(),
            json={"children": batch},
            timeout=30,
        )
        if not append.ok:
            logger.error(
                f"[yt-analysis] Block append failed {append.status_code} "
                f"(page created, transcript partial): {append.text[:300]}"
            )
            break

    return page_id


def _notify_telegram(chat_id: Optional[int], text: str) -> None:
    """Best-effort Telegram notification (used for failure visibility)."""
    if not chat_id or not settings.TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        logger.warning(f"[yt-analysis] Telegram notify failed: {exc}")


def analyze_youtube_video(url: str, chat_id: Optional[int] = None) -> None:
    """
    Full Phase 4 pipeline for one YouTube URL. Designed to run as a FastAPI
    background task — logs everything, raises nothing.

    Args:
        url: The YouTube URL to analyze.
        chat_id: Telegram chat to notify on FAILURE (success is visible in
            Notion; failures would otherwise be silent server-log entries).
    """
    if not settings.YOUTUBE_ANALYSIS_ENABLED:
        return
    if not settings.OPENAI_API_KEY or not settings.NOTION_YOUTUBE_DB_ID:
        logger.info("[yt-analysis] Skipped — OPENAI_API_KEY / NOTION_YOUTUBE_DB_ID not configured")
        return

    if _already_analyzed(url):
        logger.info(f"[yt-analysis] Already analyzed — skipping: {url}")
        return

    logger.info(f"[yt-analysis] Starting pipeline for {url}")

    transcript = fetch_transcript(url)
    if not transcript.success:
        logger.error(f"[yt-analysis] Transcript fetch failed for {url}: {transcript.error}")
        _notify_telegram(chat_id, f"⚠️ Transcript unavailable, analysis skipped:\n{url}\n({transcript.error})")
        return

    analysis = analyze_transcript(
        transcript.text,
        video_title=transcript.title,
        channel=transcript.channel,
    )
    if not analysis.success:
        logger.error(f"[yt-analysis] LLM analysis failed for {url}: {analysis.error}")
        _notify_telegram(chat_id, f"⚠️ Analysis failed:\n{url}\n({analysis.error})")
        return

    page_id = _save_analysis_to_notion(url, transcript, analysis)
    if page_id:
        logger.info(f"[yt-analysis] Saved analysis for {url} (page_id={page_id})")
    else:
        _notify_telegram(chat_id, f"⚠️ Analysis done but Notion save failed:\n{url}\n(see server logs)")
