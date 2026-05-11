"""
Centralised configuration loaded from environment variables / .env file.

All other modules import `settings` from here — never read os.environ directly
outside this file. That makes it easy to swap the source (e.g., secrets manager)
in one place later.
"""

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv

# Load .env file if present (no-op in production where env vars are set directly)
load_dotenv()


class Settings:
    # ------------------------------------------------------------------
    # WeCom (Enterprise WeChat) callback configuration
    # ------------------------------------------------------------------

    # Token: the same string you fill in on the WeCom developer console.
    # Used to verify that incoming requests really come from WeCom.
    WECOM_TOKEN: str = os.getenv("WECOM_TOKEN", "")

    # AES key for message encryption/decryption (43 chars, Base64-encoded).
    # Required only when "encrypted mode" is enabled on WeCom console.
    WECOM_ENCODING_AES_KEY: str = os.getenv("WECOM_ENCODING_AES_KEY", "")

    # Your WeCom Corp ID (企业ID)
    WECOM_CORP_ID: str = os.getenv("WECOM_CORP_ID", "")

    # Corp Secret — used to fetch access_token for outbound API calls
    # (sending messages back to users, etc.)
    WECOM_CORP_SECRET: str = os.getenv("WECOM_CORP_SECRET", "")

    # Agent ID of the self-built app (visible in admin backend)
    WECOM_AGENT_ID: str = os.getenv("WECOM_AGENT_ID", "")

    # ------------------------------------------------------------------
    # Notion API configuration
    # ------------------------------------------------------------------

    # Notion integration secret — starts with "secret_"
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")

    # The ID of the database where links will be stored.
    # Found in the database URL: notion.so/<workspace>/<DATABASE_ID>?v=...
    NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # iOS Shortcut / collect endpoint API key
    # ------------------------------------------------------------------

    # Any random string you choose — must match what the iOS Shortcut sends.
    # Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    COLLECT_API_KEY: str = os.getenv("COLLECT_API_KEY", "")

    # ------------------------------------------------------------------
    # Telegram Bot configuration (alternative input channel to WeCom)
    # ------------------------------------------------------------------

    # Bot token from @BotFather on Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # Optional: comma-separated list of allowed Telegram user IDs (numeric).
    # Leave blank to allow any user — lock it down once the bot is working.
    # Example: "123456789,987654321"
    TELEGRAM_ALLOWED_USERS: str = os.getenv("TELEGRAM_ALLOWED_USERS", "")

    # ------------------------------------------------------------------
    # Application settings
    # ------------------------------------------------------------------

    # "development" or "production"
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # Log level: DEBUG, INFO, WARNING, ERROR
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Host / port for local uvicorn (used by the run instructions)
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # ------------------------------------------------------------------
    # Phase 3 — media downloader (yt-dlp)
    # ------------------------------------------------------------------

    # Where downloaded media files are saved (inside the container).
    # On Hetzner, mount a host directory here:
    #   docker run ... -v /root/video-output:/data/video-output ...
    VIDEO_OUTPUT_DIR: str = os.getenv("VIDEO_OUTPUT_DIR", "/data/video-output")

    # Default max video resolution for yt-dlp downloads ("720", "1080", "best").
    # Audio-only mode is triggered per-call (e.g. /dl audio <url>) regardless.
    DOWNLOAD_MAX_HEIGHT: str = os.getenv("DOWNLOAD_MAX_HEIGHT", "720")

    # Whether to start the Notion Status poller on app startup.
    # Set to "false" if you want only the Telegram /dl trigger.
    DOWNLOAD_POLLER_ENABLED: bool = os.getenv("DOWNLOAD_POLLER_ENABLED", "true").lower() == "true"

    # How often the poller checks Notion for rows with Download Status = Requested.
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

    # Hard timeout for a single yt-dlp invocation (seconds). Long videos with
    # slow upstream can take a while; default 10 minutes.
    DOWNLOAD_TIMEOUT_SECONDS: int = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "600"))

    # Cobalt API endpoint — for YouTube downloads that bypass yt-dlp's
    # data-center-IP block. Cobalt runs as a sibling container on n8n_default.
    # Override only if you change the cobalt container name or port.
    COBALT_API_URL: str = os.getenv("COBALT_API_URL", "http://cobalt-api:9000/")

    # Residential proxy for downloader outbound traffic.
    # Set only if YouTube blocks your server's IP — your home/laptop IP works
    # fine without a proxy.
    # Format: "http://user:pass@host:port" or "socks5://user:pass@host:port"
    # When set, yt-dlp passes this via --proxy. Cobalt should also be started
    # with API_EXTERNAL_PROXY=<same value> (env var on the cobalt container).
    PROXY_URL: str = os.getenv("PROXY_URL", "")

    def validate(self) -> None:
        """
        Raise an error at startup if required variables are missing.
        Call this from main.py on application startup.
        """
        missing = []
        if not self.NOTION_API_KEY:
            missing.append("NOTION_API_KEY")
        if not self.NOTION_DATABASE_ID:
            missing.append("NOTION_DATABASE_ID")

        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in the values."
            )


# Single shared instance — import this everywhere
settings = Settings()


def configure_logging() -> None:
    """Apply the log level from settings to the root logger."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level)
    logging.getLogger("uvicorn.access").setLevel(level)
