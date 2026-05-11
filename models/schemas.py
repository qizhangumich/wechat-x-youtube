"""
Pydantic models (schemas) shared across the application.

Keeping all data models in one place makes it easy to see the full
data contract and to extend fields later (e.g., for Step 2 / Step 3).
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    """Platform / source classification for a captured link."""
    YOUTUBE = "youtube"
    TWITTER = "twitter"
    WECHAT_OFFICIAL_ACCOUNT = "wechat_official_account"
    WECHAT_CHANNEL = "wechat_channel"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Internal domain models
# ---------------------------------------------------------------------------

class ExtractedLink(BaseModel):
    """A single URL extracted from a message, with its identified platform."""
    url: str
    source_type: SourceType


class WeComTextMessage(BaseModel):
    """
    Represents a parsed WeCom text message after XML decoding.

    WeCom delivers messages as XML; this model holds the fields we care about.
    Optional fields default to empty strings so callers don't have to guard
    against missing keys — WeCom's schema can vary slightly across message types.
    """
    to_user: str = ""
    from_user: str = ""
    create_time: int = 0
    msg_type: str = "text"
    content: str = ""
    msg_id: str = ""
    agent_id: str = ""


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class TestMessageRequest(BaseModel):
    """
    Payload for the local test endpoint POST /webhook/wecom/test.
    Allows end-to-end testing without a real WeCom callback.
    """
    text: str = Field(..., description="Raw text that may contain URLs")


class LinkSaveResult(BaseModel):
    """Result for a single URL after attempting to write it to Notion."""
    url: str
    source_type: SourceType
    status: str  # "saved" | "duplicate" | "error"
    notion_page_id: Optional[str] = None
    error: Optional[str] = None


class ProcessResult(BaseModel):
    """
    Top-level response returned by the processing pipeline for a message.
    Returned by both the WeCom callback and the test endpoint.
    """
    success: bool
    links_found: int
    links_saved: int
    links_skipped: int  # duplicates + errors
    results: List[LinkSaveResult] = []
    message: str = ""
