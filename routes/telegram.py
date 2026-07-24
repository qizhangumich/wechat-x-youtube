"""
Telegram Bot webhook endpoint.

Telegram sends a POST to /webhook/telegram every time someone sends a
message to your bot.  No ICP filing, no signature complexity — just a
secret token in the URL path to prevent random people from calling it.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → get TELEGRAM_BOT_TOKEN
  2. Set TELEGRAM_BOT_TOKEN in .env
  3. Run the app, then call /webhook/telegram/set_webhook to register
     the public URL with Telegram (needs ngrok or a real domain)

Workflow:
  User forwards any message (WeChat link, tweet, YouTube, etc.) to the bot
  → Telegram POSTs to this endpoint
  → We extract URLs from the message text
  → Save each URL to Notion
  → Reply to the user with a summary
"""

from typing import Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import ProcessResult
from services.downloader import download_url
from services.message_service import process_message
from services.notion_client import NotionClient, get_notion_client
from services.youtube_analysis import analyze_youtube_video
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook/telegram", tags=["telegram"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_text_from_update(update: dict) -> str:
    """
    Extract plain text from a Telegram Update object.
    Handles regular messages and forwarded messages.
    Combines caption + text so forwarded posts with captions work too.
    """
    message = update.get("message") or update.get("channel_post") or {}

    parts = []
    # text covers regular messages and forwarded text
    if message.get("text"):
        parts.append(message["text"])
    # caption covers forwarded posts with a photo/video + text
    if message.get("caption"):
        parts.append(message["caption"])

    # Also grab any URL entities explicitly listed by Telegram
    # (useful when the URL is embedded but not visible as plain text)
    for entity_type in ("entities", "caption_entities"):
        for entity in message.get(entity_type, []):
            if entity.get("type") == "url":
                # Extract the URL substring from the text
                text_source = message.get("text") or message.get("caption") or ""
                offset = entity["offset"]
                length = entity["length"]
                url = text_source[offset: offset + length]
                if url and url not in " ".join(parts):
                    parts.append(url)

    return "\n".join(parts)


def _is_allowed_user(update: dict) -> bool:
    """
    Return True if the sender is in the allowed-users list.
    If TELEGRAM_ALLOWED_USERS is empty, everyone is allowed.
    """
    if not settings.TELEGRAM_ALLOWED_USERS.strip():
        return True  # open access

    allowed_ids = {
        uid.strip()
        for uid in settings.TELEGRAM_ALLOWED_USERS.split(",")
        if uid.strip()
    }

    message = update.get("message") or update.get("channel_post") or {}
    sender  = message.get("from") or {}
    user_id = str(sender.get("id", ""))
    return user_id in allowed_ids


async def _send_telegram_reply(chat_id: int, text: str) -> None:
    """
    Send a reply message back to the user via Telegram Bot API.
    Fire-and-forget — errors are logged but don't affect the response.
    """
    import requests as req
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        resp = req.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        logger.error(f"Failed to send Telegram reply: {exc}")


def _build_reply(result: ProcessResult) -> str:
    """Format a human-readable summary to send back to the user."""
    if result.links_found == 0:
        return "⚠️ No links found in your message."

    lines = [f"✅ Found <b>{result.links_found}</b> link(s):"]
    for r in result.results:
        icon = {"saved": "💾", "duplicate": "🔁", "error": "❌"}.get(r.status, "•")
        platform = r.source_type.value.replace("_", " ").title()
        lines.append(f"{icon} [{platform}] {r.url[:60]}{'...' if len(r.url) > 60 else ''}")

    lines.append("")
    lines.append(
        f"Saved: <b>{result.links_saved}</b>  |  "
        f"Duplicates: <b>{sum(1 for r in result.results if r.status == 'duplicate')}</b>  |  "
        f"Errors: <b>{sum(1 for r in result.results if r.status == 'error')}</b>"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _parse_dl_command(text: str) -> Optional[Tuple[str, bool]]:
    """
    Parse a `/dl` or `/dla` command. Returns (url, audio_only) or None if not a command.

    Accepted forms:
        /dl https://...               → (url, audio_only=False)
        /dl audio https://...         → (url, audio_only=True)
        /dla https://...              → (url, audio_only=True)   shorthand
        /dl@botname https://...       → also supported (group chat addressing)
    """
    stripped = text.strip()
    parts = stripped.split(maxsplit=2)
    if not parts:
        return None

    cmd = parts[0].lower()
    # Strip group-chat @bot suffix: /dl@wechatxyoutube_bot → /dl
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if cmd == "/dl":
        if len(parts) >= 3 and parts[1].lower() == "audio":
            return parts[2].strip(), True
        if len(parts) >= 2:
            return parts[1].strip(), False
        return None  # /dl with no URL — fall through to no-action
    if cmd == "/dla" and len(parts) >= 2:
        return parts[1].strip(), True
    return None


def _download_in_background(url: str, page_id: Optional[str], audio_only: bool) -> None:
    """
    Run yt-dlp and write the result back to Notion.
    Called via FastAPI BackgroundTasks — runs after the HTTP response is sent.
    Synchronous on purpose; BackgroundTasks executes in a thread pool.
    """
    notion = get_notion_client()
    result = download_url(url, audio_only=audio_only)

    if not page_id:
        # The URL wasn't saveable (parser found nothing) — just log
        logger.info(f"[telegram /dl] Download finished for {url} — no Notion page to update")
        return

    try:
        if result.success:
            notion.update_page(page_id, NotionClient.build_download_done_props(
                file_path=result.file_path,
                size_mb=result.file_size_mb or 0.0,
                duration=result.duration,
            ))
            logger.info(f"[telegram /dl] Saved {url} → {result.file_path}")
        else:
            notion.update_page(page_id, NotionClient.build_download_failed_props(
                result.error or "Unknown error"
            ))
            logger.error(f"[telegram /dl] Download failed for {url}: {result.error}")
    except Exception as exc:
        logger.error(f"[telegram /dl] Notion update failed for {page_id}: {exc}")


def _schedule_youtube_analysis(
    background_tasks: BackgroundTasks,
    result: ProcessResult,
    chat_id: Optional[int] = None,
) -> None:
    """
    Phase 4: for every YouTube link in a processed message, schedule the
    transcript → LLM → youtube-analysis-DB pipeline as a background task.
    Duplicates are fine — the pipeline dedupes against the analysis DB itself,
    so re-shared videos don't re-analyze. chat_id is used to notify the sender
    on failure (success shows up in Notion by itself).
    """
    for r in result.results:
        if r.source_type.value == "youtube":
            background_tasks.add_task(analyze_youtube_video, r.url, chat_id)
            logger.info(f"[telegram] YouTube analysis scheduled for {r.url}")


@router.post("")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Receive Telegram update and process any links in the message.
    Telegram expects HTTP 200 regardless of outcome.
    """
    try:
        update: dict = await request.json()
    except Exception:
        # Malformed JSON — ack anyway so Telegram stops retrying
        return JSONResponse({"ok": True})

    logger.debug(f"Telegram update received: {str(update)[:200]}")

    # Access control
    if not _is_allowed_user(update):
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id:
            await _send_telegram_reply(chat_id, "⛔ You are not authorised to use this bot.")
        logger.warning(f"Rejected unauthorised Telegram update")
        return JSONResponse({"ok": True})

    # Extract text
    text = _get_text_from_update(update)
    message = update.get("message") or update.get("channel_post") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not text.strip():
        logger.info("Telegram update has no text — skipping")
        return JSONResponse({"ok": True})

    logger.info(f"Telegram message from chat {chat_id}: {text[:120]}")

    # --- /dl command path -----------------------------------------------
    # /dl <url> or /dl audio <url> → save to Notion AND schedule download
    parsed = _parse_dl_command(text)
    if parsed is not None:
        url, audio_only = parsed

        # Save first via the normal pipeline so dedup + Notion row creation happens
        try:
            result = process_message(url, captured_from="Telegram")
        except Exception as exc:
            logger.error(f"[telegram /dl] Pipeline error: {exc}", exc_info=True)
            if chat_id:
                await _send_telegram_reply(chat_id, "❌ Could not save link. See server logs.")
            return JSONResponse({"ok": True})

        if result.links_found == 0:
            if chat_id:
                await _send_telegram_reply(chat_id, "⚠️ No URL found after `/dl`.")
            return JSONResponse({"ok": True})

        # Find a page_id to attach the download result to.
        # For new saves it's on the LinkSaveResult; for duplicates we look it up.
        page_id: Optional[str] = None
        for r in result.results:
            if r.notion_page_id:
                page_id = r.notion_page_id
                break

        if not page_id:
            # Likely a duplicate URL — look up the existing row so we can
            # still update its Download Status / Local File.
            page_id = get_notion_client().find_page_id_by_url(url)

        # Schedule the actual download — runs after this response goes out
        background_tasks.add_task(_download_in_background, url, page_id, audio_only)

        # Phase 4: YouTube links also get transcript analysis
        _schedule_youtube_analysis(background_tasks, result, chat_id)

        mode = "🎵 audio" if audio_only else "🎬 video (≤720p)"
        link_status = "🔁 already saved" if result.links_saved == 0 else "💾 saved"
        if chat_id:
            await _send_telegram_reply(
                chat_id,
                f"📥 {mode} download queued.\n{link_status} to Notion. "
                f"File will appear in the row's Local File field when ready (~30s–10min).",
            )
        return JSONResponse({"ok": True})

    # --- Default path: just save link(s) to Notion (no download) -------
    try:
        result = process_message(text, captured_from="Telegram")

        # Phase 4: YouTube links get transcript analysis automatically
        _schedule_youtube_analysis(background_tasks, result, chat_id)

        if chat_id:
            await _send_telegram_reply(chat_id, _build_reply(result))
    except Exception as exc:
        logger.error(f"Pipeline error for Telegram message: {exc}", exc_info=True)
        if chat_id:
            await _send_telegram_reply(chat_id, "❌ An error occurred. Please try again.")

    return JSONResponse({"ok": True})


@router.get("/set_webhook")
async def set_webhook(public_url: str) -> JSONResponse:
    """
    One-time setup: register your public URL with Telegram.

    Call this once after starting the server with a public URL:
      GET /webhook/telegram/set_webhook?public_url=https://your-domain.com

    Telegram will then POST to https://your-domain.com/webhook/telegram
    for every message.
    """
    import requests as req

    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")

    webhook_url = f"{public_url.rstrip('/')}/webhook/telegram"
    api_url     = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"

    try:
        resp = req.post(api_url, json={"url": webhook_url}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Telegram webhook set to: {webhook_url} — response: {data}")
        return JSONResponse({"ok": True, "webhook_url": webhook_url, "telegram_response": data})
    except Exception as exc:
        logger.error(f"Failed to set Telegram webhook: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/info")
async def webhook_info() -> JSONResponse:
    """Check what webhook URL is currently registered with Telegram."""
    import requests as req

    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")

    api_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    try:
        resp = req.get(api_url, timeout=10)
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
