"""
Message processing pipeline — the core orchestration layer.

Responsibilities:
  1. Accept raw message text
  2. Delegate URL extraction to link_parser
  3. Delegate Notion writes to notion_client
  4. Return a structured ProcessResult

This service is intentionally decoupled from HTTP concerns (FastAPI) so it
can be called from the WeCom callback, the test endpoint, or a future CLI/
background worker without modification.

Extension points for Step 2 / Step 3:
  - After saving to Notion, publish an enrichment task (e.g. to a queue)
    by adding a call at the bottom of _process_link().
  - Add an optional `enrich: bool` parameter to process_message() to toggle
    enrichment on/off per-call.
"""

from typing import List

from models.schemas import ExtractedLink, LinkSaveResult, ProcessResult
from services.link_parser import parse_links
from services.notion_client import get_notion_client
from utils.logger import get_logger

logger = get_logger(__name__)


def process_message(text: str) -> ProcessResult:
    """
    Full pipeline: text → extracted links → Notion pages.

    Args:
        text: Raw message content (may contain zero or more URLs).

    Returns:
        ProcessResult with counts and per-link save results.
    """
    # --- Step 1: Extract links ------------------------------------------------
    links: List[ExtractedLink] = parse_links(text)

    if not links:
        logger.info("No links found in message — skipping.")
        return ProcessResult(
            success=True,
            links_found=0,
            links_saved=0,
            links_skipped=0,
            message="No links found in message.",
        )

    logger.info(f"Found {len(links)} link(s) — writing to Notion...")

    # --- Step 2: Save each link to Notion -------------------------------------
    notion = get_notion_client()
    results: List[LinkSaveResult] = []

    for link in links:
        result = _process_link(notion, link, raw_message=text)
        results.append(result)

    # --- Step 3: Aggregate counts --------------------------------------------
    saved   = sum(1 for r in results if r.status == "saved")
    skipped = sum(1 for r in results if r.status in ("duplicate", "error"))

    logger.info(f"Done — saved={saved}, skipped/duplicate/error={skipped}")

    return ProcessResult(
        success=True,
        links_found=len(links),
        links_saved=saved,
        links_skipped=skipped,
        results=results,
        message=f"Processed {len(links)} link(s): {saved} saved, {skipped} skipped.",
    )


def _process_link(notion, link: ExtractedLink, raw_message: str) -> LinkSaveResult:
    """
    Save a single link and return its result.

    Separated from process_message() so Step 2 can easily add enrichment
    logic here (e.g. schedule a background task after a successful save).
    """
    result = notion.save_link(link, raw_message)

    # --- Extension point: Step 2 enrichment hook ----------------------------
    # if result.status == "saved":
    #     enrichment_queue.enqueue(result.notion_page_id, link.url)

    return result
