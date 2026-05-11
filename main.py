"""
Application entry point.

Wires together FastAPI, registers routers, and runs startup validation.
Keep this file thin — business logic lives in routes/ and services/.
"""

import sys

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import configure_logging, settings
from routes.wecom import router as wecom_router
from routes.telegram import router as telegram_router
from routes.collect import router as collect_router
from utils.logger import get_logger

# Configure logging as early as possible so all subsequent imports see it
configure_logging()
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Create the FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WeCom → Notion Link Collector",
    description=(
        "Receives WeCom messages, extracts URLs, "
        "identifies their source platform, and saves them to a Notion database."
    ),
    version="1.0.0",
    # Disable docs in production to reduce attack surface (optional)
    # docs_url=None if settings.APP_ENV == "production" else "/docs",
)


# ---------------------------------------------------------------------------
# Startup / shutdown events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    logger.info(f"Starting WeCom Collector (env={settings.APP_ENV})")
    try:
        settings.validate()
        logger.info("Configuration OK")
    except EnvironmentError as exc:
        logger.error(f"Configuration error: {exc}")
        # Exit hard so the problem is visible immediately rather than failing
        # on the first real request with a confusing traceback.
        sys.exit(1)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("WeCom Collector shutting down")


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(wecom_router)
app.include_router(telegram_router)
app.include_router(collect_router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
async def health_check() -> JSONResponse:
    """
    Simple liveness probe.
    Returns 200 when the service is running and configuration is present.
    """
    notion_configured   = bool(settings.NOTION_API_KEY and settings.NOTION_DATABASE_ID)
    wecom_configured    = bool(settings.WECOM_TOKEN)
    telegram_configured = bool(settings.TELEGRAM_BOT_TOKEN)

    return JSONResponse({
        "status": "ok",
        "env": settings.APP_ENV,
        "notion_configured": notion_configured,
        "wecom_token_set": wecom_configured,
        "telegram_bot_set": telegram_configured,
    })


# ---------------------------------------------------------------------------
# Run directly with `python main.py`  (alternative to uvicorn CLI)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=(settings.APP_ENV == "development"),
        log_level=settings.LOG_LEVEL.lower(),
    )
