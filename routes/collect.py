"""
/collect  — Universal link collection endpoint.

Called by the iOS Shortcut from the WeChat Share Sheet (or any other
client that can send an HTTP POST). Protected by a simple API key so
random people can't spam your Notion database.

Endpoint:  POST /collect
Headers:   X-API-Key: <your COLLECT_API_KEY from .env>
Body:      { "text": "https://mp.weixin.qq.com/s/abc123" }
           OR
           { "text": "Title\nhttps://mp.weixin.qq.com/s/abc123\nsome description" }

The body can be a raw URL or any text containing URLs — the parser
extracts them automatically. This means you can send the full share
sheet text from iOS (title + URL + description) without pre-processing.

Response (success):
{
  "success": true,
  "links_found": 1,
  "links_saved": 1,
  "links_skipped": 0,
  "results": [{ "url": "...", "source_type": "wechat_official_account", "status": "saved" }],
  "message": "Processed 1 link(s): 1 saved, 0 skipped."
}
"""

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from config import settings
from models.schemas import ProcessResult
from services.message_service import process_message
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/collect", tags=["collect"])


class CollectRequest(BaseModel):
    """Request body — plain text (may contain URLs anywhere inside it)."""
    text: str


@router.post("", response_model=ProcessResult)
async def collect(
    payload: CollectRequest,
    x_api_key: str = Header(..., description="API key set in COLLECT_API_KEY env var"),
) -> ProcessResult:
    """
    Receive a link (or any text) and save all URLs found to Notion.

    Called by the iOS Shortcut from the WeChat share sheet.
    Requires X-API-Key header matching COLLECT_API_KEY in .env.
    """
    # --- Auth check -----------------------------------------------------------
    expected = settings.COLLECT_API_KEY.strip()
    if not expected:
        # If no API key configured, block all requests to avoid open access
        logger.error("COLLECT_API_KEY not set — rejecting request")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server not configured (COLLECT_API_KEY missing)",
        )

    if x_api_key.strip() != expected:
        logger.warning("collect: invalid API key attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # --- Process --------------------------------------------------------------
    logger.info(f"collect: received text ({len(payload.text)} chars): {payload.text[:120]}")
    result = process_message(payload.text, captured_from="iOS Shortcut")
    return result
