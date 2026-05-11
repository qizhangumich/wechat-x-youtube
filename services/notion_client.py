"""
Notion API client.

Responsible for all communication with the Notion API:
  - Checking for duplicate pages (dedup)
  - Creating new pages in the collection database

Extension points for Step 2 / Step 3:
  - update_page()  → add extracted content, summary, title after enrichment
  - All field names are defined as constants at the top; rename them here if
    your Notion database uses different column names.
"""

import hashlib
from datetime import datetime, timezone
from typing import Optional

import requests

from config import settings
from models.schemas import ExtractedLink, LinkSaveResult, SourceType
from services.link_parser import normalize_url
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion database property names — change here if your DB uses different labels
PROP_TITLE         = "Title"
PROP_ORIGINAL_URL  = "Original URL"
PROP_SOURCE_TYPE   = "Source Type"
PROP_PLATFORM      = "Platform"
PROP_CAPTURED_FROM = "Captured From"
PROP_RAW_MESSAGE   = "Raw Message"
PROP_STATUS        = "Status"
PROP_CREATED_AT    = "Created At"
PROP_DEDUP_KEY     = "Dedup Key"
PROP_NOTES         = "Notes"

# Human-readable platform labels
PLATFORM_LABELS = {
    SourceType.YOUTUBE:                 "YouTube",
    SourceType.TWITTER:                 "Twitter / X",
    SourceType.WECHAT_OFFICIAL_ACCOUNT: "WeChat Official Account",
    SourceType.WECHAT_CHANNEL:          "WeChat Channels",
    SourceType.OTHER:                   "Other",
}

# Notion rich_text max length
NOTION_TEXT_LIMIT = 2000


# ---------------------------------------------------------------------------
# Helper builders for Notion property payloads
# ---------------------------------------------------------------------------

def _rich_text(value: str) -> list:
    """Truncate and wrap a string as a Notion rich_text array."""
    truncated = value[:NOTION_TEXT_LIMIT] if len(value) > NOTION_TEXT_LIMIT else value
    return [{"text": {"content": truncated}}]


def _title(value: str) -> list:
    truncated = value[:255] if len(value) > 255 else value
    return [{"text": {"content": truncated}}]


# ---------------------------------------------------------------------------
# NotionClient
# ---------------------------------------------------------------------------

class NotionClient:
    """
    Thin wrapper around the Notion REST API.

    Instantiate once (e.g. as a module-level singleton or FastAPI dependency)
    and reuse across requests.
    """

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        }
        self._db_id = settings.NOTION_DATABASE_ID

    # ------------------------------------------------------------------
    # Dedup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_dedup_key(url: str) -> str:
        """
        Produce a stable, compact key for a URL.
        Uses MD5 of the normalised URL — collision risk is acceptable for
        this use-case (we'd just skip saving a truly unique link, not lose data).
        """
        normalized = normalize_url(url)
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def is_duplicate(self, url: str) -> bool:
        """
        Return True if a page with the same dedup_key already exists in Notion.
        Returns False on API error to allow the save attempt to proceed.
        """
        dedup_key = self.build_dedup_key(url)
        endpoint = f"{NOTION_API_BASE}/databases/{self._db_id}/query"
        payload = {
            "filter": {
                "property": PROP_DEDUP_KEY,
                "rich_text": {"equals": dedup_key},
            },
            "page_size": 1,  # we only need to know if ≥1 exists
        }
        try:
            resp = requests.post(endpoint, headers=self._headers, json=payload, timeout=10)
            resp.raise_for_status()
            return len(resp.json().get("results", [])) > 0
        except requests.RequestException as exc:
            logger.error(f"Notion dedup check failed for '{url}': {exc}")
            return False  # fail open — attempt to save

    # ------------------------------------------------------------------
    # Page creation
    # ------------------------------------------------------------------

    def _build_page_payload(self, link: ExtractedLink, raw_message: str) -> dict:
        """Construct the full Notion page creation payload."""
        dedup_key = self.build_dedup_key(link.url)
        platform_label = PLATFORM_LABELS.get(link.source_type, "Other")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        return {
            "parent": {"database_id": self._db_id},
            "properties": {
                PROP_TITLE: {
                    "title": _title(link.url),
                },
                PROP_ORIGINAL_URL: {
                    "url": link.url,
                },
                PROP_SOURCE_TYPE: {
                    # "select" — value must match an existing option or Notion
                    # will create a new option automatically.
                    "select": {"name": link.source_type.value},
                },
                PROP_PLATFORM: {
                    "rich_text": _rich_text(platform_label),
                },
                PROP_CAPTURED_FROM: {
                    "select": {"name": "WeCom"},
                },
                PROP_RAW_MESSAGE: {
                    "rich_text": _rich_text(raw_message),
                },
                PROP_STATUS: {
                    "select": {"name": "Inbox"},
                },
                PROP_CREATED_AT: {
                    "date": {"start": now_iso},
                },
                PROP_DEDUP_KEY: {
                    "rich_text": _rich_text(dedup_key),
                },
                PROP_NOTES: {
                    "rich_text": [],  # empty for now; Step 3 will fill this
                },
            },
        }

    def create_page(self, link: ExtractedLink, raw_message: str) -> dict:
        """
        Create a new page in the Notion database.
        Raises requests.HTTPError on failure.
        """
        payload = self._build_page_payload(link, raw_message)
        resp = requests.post(
            f"{NOTION_API_BASE}/pages",
            headers=self._headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public save interface
    # ------------------------------------------------------------------

    def save_link(self, link: ExtractedLink, raw_message: str) -> LinkSaveResult:
        """
        Save a single link to Notion with dedup protection.

        Returns a LinkSaveResult describing what happened:
          - status="saved"      → new page created
          - status="duplicate"  → skipped, already exists
          - status="error"      → API call failed
        """
        try:
            if self.is_duplicate(link.url):
                logger.info(f"[notion] Duplicate — skipped: {link.url}")
                return LinkSaveResult(
                    url=link.url,
                    source_type=link.source_type,
                    status="duplicate",
                )

            page = self.create_page(link, raw_message)
            page_id = page.get("id", "")
            logger.info(f"[notion] Saved: {link.url} (page_id={page_id})")
            return LinkSaveResult(
                url=link.url,
                source_type=link.source_type,
                status="saved",
                notion_page_id=page_id,
            )

        except requests.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            logger.error(f"[notion] HTTP error saving '{link.url}': {exc} — {body}")
            return LinkSaveResult(
                url=link.url,
                source_type=link.source_type,
                status="error",
                error=str(exc),
            )
        except Exception as exc:
            logger.error(f"[notion] Unexpected error saving '{link.url}': {exc}")
            return LinkSaveResult(
                url=link.url,
                source_type=link.source_type,
                status="error",
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, created on first import after config is ready)
# ---------------------------------------------------------------------------

_client: Optional[NotionClient] = None


def get_notion_client() -> NotionClient:
    """
    Return the shared NotionClient instance.
    Use this as a FastAPI dependency or call directly from services.
    """
    global _client
    if _client is None:
        _client = NotionClient()
    return _client
