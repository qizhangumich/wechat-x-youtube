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
from models.schemas import ExtractedLink, LinkSaveResult
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
PROP_CAPTURED_FROM = "Captured From"
PROP_RAW_MESSAGE   = "Raw Message"
PROP_STATUS        = "Status"
PROP_CREATED_AT    = "Created At"
PROP_DEDUP_KEY     = "Dedup Key"

# --- Phase 3 — download properties (created manually in Notion UI) -----------
PROP_DOWNLOAD_STATUS = "Download Status"  # Select: Requested | Downloading | Done | Failed
PROP_LOCAL_FILE      = "Local File"        # Text — path on Hetzner filesystem
PROP_FILE_SIZE_MB    = "File Size MB"      # Number
PROP_DURATION        = "Duration"          # Text — e.g. "12:34"
PROP_DOWNLOADED_AT   = "Downloaded At"     # Date
PROP_DOWNLOAD_ERROR  = "Download Error"    # Text

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

    # ------------------------------------------------------------------
    # Generic update / query (Phase 3 enrichment + downloader)
    # ------------------------------------------------------------------

    def update_page(self, page_id: str, properties: dict) -> dict:
        """
        Patch a Notion page with new property values.

        Args:
            page_id: Notion page ID (with or without dashes — Notion accepts both).
            properties: Already-formatted Notion property payloads. Use the
                build_download_*_props() helpers below for download-result writes.

        Raises:
            requests.HTTPError on API failure. The exception's response body is
            also logged so callers don't need to inspect it themselves.
        """
        resp = requests.patch(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=self._headers,
            json={"properties": properties},
            timeout=15,
        )
        if not resp.ok:
            # Log Notion's actual error body — invaluable for diagnosing 400s
            # (e.g. "property X doesn't exist", "select option Y missing", etc.)
            logger.error(
                f"[notion] update_page {page_id} → {resp.status_code}: {resp.text[:600]}"
            )
        resp.raise_for_status()
        return resp.json()

    def query_pages(self, filter_obj: dict, page_size: int = 100) -> list:
        """
        Query the configured database with a Notion filter object.
        Returns the list of matching page records.
        """
        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{self._db_id}/query",
            headers=self._headers,
            json={"filter": filter_obj, "page_size": page_size},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    # ------------------------------------------------------------------
    # Download-result property builders (used by downloader caller code)
    # ------------------------------------------------------------------

    @staticmethod
    def build_download_in_progress_props() -> dict:
        """Mark a row as actively downloading — prevents the poller picking it up twice."""
        return {
            PROP_DOWNLOAD_STATUS: {"select": {"name": "Downloading"}},
        }

    @staticmethod
    def build_download_done_props(
        file_path: str,
        size_mb: float,
        duration: Optional[str] = None,
    ) -> dict:
        """Notion property payload for a successful download."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        props = {
            PROP_DOWNLOAD_STATUS: {"select": {"name": "Done"}},
            PROP_LOCAL_FILE: {"rich_text": _rich_text(file_path)},
            PROP_FILE_SIZE_MB: {"number": size_mb},
            PROP_DOWNLOADED_AT: {"date": {"start": now_iso}},
            # Clear any prior error message
            PROP_DOWNLOAD_ERROR: {"rich_text": []},
        }
        if duration:
            props[PROP_DURATION] = {"rich_text": _rich_text(duration)}
        return props

    @staticmethod
    def build_download_failed_props(error: str) -> dict:
        """Notion property payload for a failed download — store error for debugging."""
        return {
            PROP_DOWNLOAD_STATUS: {"select": {"name": "Failed"}},
            PROP_DOWNLOAD_ERROR: {"rich_text": _rich_text(error[:1900])},
        }

    # ------------------------------------------------------------------
    # Dedup helpers
    # ------------------------------------------------------------------

    def is_duplicate(self, url: str) -> bool:
        """
        Return True if a page with the same dedup_key already exists in Notion.
        Returns False on API error to allow the save attempt to proceed.
        """
        return self.find_page_id_by_url(url) is not None

    def find_page_id_by_url(self, url: str) -> Optional[str]:
        """
        Look up an existing Notion page by URL (via dedup key). Returns the
        page ID if found, None otherwise. Used by /dl on already-saved URLs
        so the download result can still be written back to the existing row.
        """
        dedup_key = self.build_dedup_key(url)
        endpoint = f"{NOTION_API_BASE}/databases/{self._db_id}/query"
        payload = {
            "filter": {
                "property": PROP_DEDUP_KEY,
                "rich_text": {"equals": dedup_key},
            },
            "page_size": 1,
        }
        try:
            resp = requests.post(endpoint, headers=self._headers, json=payload, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return results[0]["id"] if results else None
        except requests.RequestException as exc:
            logger.error(f"Notion dedup check failed for '{url}': {exc}")
            return None  # fail open — caller can attempt to save

    # ------------------------------------------------------------------
    # Page creation
    # ------------------------------------------------------------------

    def _build_page_payload(
        self,
        link: ExtractedLink,
        raw_message: str,
        captured_from: str = "WeCom",
    ) -> dict:
        """Construct the full Notion page creation payload."""
        dedup_key = self.build_dedup_key(link.url)
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
                PROP_CAPTURED_FROM: {
                    # Which ingress channel saved this link — set per-route by
                    # the caller (WeCom on SCF, Telegram on Hetzner, etc.)
                    "select": {"name": captured_from},
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
            },
        }

    def create_page(
        self,
        link: ExtractedLink,
        raw_message: str,
        captured_from: str = "WeCom",
    ) -> dict:
        """
        Create a new page in the Notion database.
        Raises requests.HTTPError on failure.
        """
        payload = self._build_page_payload(link, raw_message, captured_from)
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

    def save_link(
        self,
        link: ExtractedLink,
        raw_message: str,
        captured_from: str = "WeCom",
    ) -> LinkSaveResult:
        """
        Save a single link to Notion with dedup protection.

        Args:
            link: The extracted link to save.
            raw_message: Original message text (saved to "Raw Message" prop).
            captured_from: Ingress-channel label written to "Captured From"
                Notion select property. Pass "Telegram", "WeCom", "iOS Shortcut",
                etc. depending on which route is calling.

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

            page = self.create_page(link, raw_message, captured_from)
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
