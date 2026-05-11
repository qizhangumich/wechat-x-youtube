"""
Notion download poller — periodic background task.

Watches the Notion database for rows where Download Status = "Requested",
runs yt-dlp on each, and writes the result back to the page.

Started on app startup from main.py. Runs as a single asyncio task; if the
process restarts, the loop restarts cleanly because pending rows are looked
up fresh from Notion (no in-memory state to lose).

Status state machine:
    (empty / None)        ← row created normally, no download requested
        │
        │ user changes status in Notion UI
        ▼
    Requested             ← poller picks up
        │
        ▼
    Downloading           ← prevents re-pickup on next poll
        │
        ├─→ Done           (success — adds Local File, File Size, Duration)
        └─→ Failed         (failure — adds Download Error)
"""

import asyncio

from config import settings
from services.downloader import download_url
from services.notion_client import (
    NotionClient,
    PROP_DOWNLOAD_STATUS,
    PROP_ORIGINAL_URL,
    get_notion_client,
)
from utils.logger import get_logger

logger = get_logger(__name__)


async def poll_loop() -> None:
    """
    Long-running asyncio task: poll Notion every N seconds for pending downloads.
    Spawned by main.py via asyncio.create_task().
    """
    interval = settings.POLL_INTERVAL_SECONDS
    logger.info(f"[poller] Starting Notion download poller (interval={interval}s)")

    notion = get_notion_client()

    while True:
        try:
            # Run the synchronous Notion + yt-dlp work in a worker thread so
            # we don't block the event loop (FastAPI's Telegram webhook etc.).
            await asyncio.to_thread(_poll_once, notion)
        except Exception as exc:
            # Never let a transient failure kill the loop
            logger.error(f"[poller] Iteration failed: {exc}", exc_info=True)
        await asyncio.sleep(interval)


def _poll_once(notion: NotionClient) -> None:
    """Single poll iteration — find Requested rows and process each."""
    try:
        pending = notion.query_pages(filter_obj={
            "property": PROP_DOWNLOAD_STATUS,
            "select": {"equals": "Requested"},
        })
    except Exception as exc:
        # Most common cause: integration not invited to DB (404 from Notion)
        logger.error(f"[poller] Could not query Notion: {exc}")
        return

    if not pending:
        return  # quiet — this is the common case

    logger.info(f"[poller] Found {len(pending)} pending download(s)")

    for page in pending:
        page_id = page.get("id", "")
        url = (page.get("properties", {}).get(PROP_ORIGINAL_URL, {}) or {}).get("url")

        if not url:
            logger.warning(f"[poller] Skipping {page_id}: no Original URL on the row")
            try:
                notion.update_page(page_id, NotionClient.build_download_failed_props(
                    "No Original URL on the row — set Download Status back to clear."
                ))
            except Exception:
                pass
            continue

        # Mark as in-progress so the next poll iteration doesn't double-process
        try:
            notion.update_page(page_id, NotionClient.build_download_in_progress_props())
        except Exception as exc:
            logger.error(f"[poller] Could not mark {page_id} as Downloading: {exc}")
            continue

        # Download (sync — blocks this worker thread, which is fine; main loop runs on)
        result = download_url(url, audio_only=False)

        try:
            if result.success:
                notion.update_page(page_id, NotionClient.build_download_done_props(
                    file_path=result.file_path,
                    size_mb=result.file_size_mb or 0.0,
                    duration=result.duration,
                ))
                logger.info(f"[poller] {url} → {result.file_path}")
            else:
                notion.update_page(page_id, NotionClient.build_download_failed_props(
                    result.error or "Unknown error"
                ))
                logger.error(f"[poller] FAILED {url}: {result.error}")
        except Exception as exc:
            # Notion update failed — file is downloaded but row won't reflect it.
            # Log loudly so you can fix the DB by hand if needed.
            logger.error(f"[poller] Could not update Notion {page_id}: {exc}")
