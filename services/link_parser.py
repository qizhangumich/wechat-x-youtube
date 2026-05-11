"""
URL extraction and platform identification.

This module is intentionally stateless — all functions are pure so they're
easy to unit-test and to reuse from other services.

Extension points for Step 2 / Step 3:
  - Add more platform rules to PLATFORM_RULES below (no other changes needed).
  - Normalise URLs before dedup (e.g. strip tracking params) inside normalize_url().
"""

import re
from typing import List
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from models.schemas import ExtractedLink, SourceType
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

# Matches http(s) URLs; stops at common trailing punctuation so we don't
# accidentally capture sentence-ending periods or Chinese brackets.
_URL_PATTERN = re.compile(
    r"https?://"                   # scheme
    r"[^\s\u3000-\u303f\uff00-\uffef"  # not CJK punctuation / whitespace
    r"<>\"'{}|\\^`\[\]]+"          # not special chars
    r"[^\s\u3000-\u303f\uff00-\uffef"  # same, but also strip trailing . , ; : ! ? ) ]
    r"<>\"'{}|\\^`\[\].,;:!?)]",
    re.IGNORECASE,
)


def extract_urls(text: str) -> List[str]:
    """
    Return all unique URLs found in *text*, preserving order of first occurrence.
    """
    if not text:
        return []
    urls = _URL_PATTERN.findall(text)
    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    logger.debug(f"Extracted {len(unique)} URL(s) from text")
    return unique


# ---------------------------------------------------------------------------
# Platform identification
# ---------------------------------------------------------------------------

# Each entry: (SourceType, list-of-domain-substrings-to-match)
# Rules are checked top-to-bottom; first match wins.
# Add new platforms here without touching any other code.
PLATFORM_RULES: List[tuple] = [
    (SourceType.YOUTUBE,                  ["youtube.com", "youtu.be"]),
    (SourceType.TWITTER,                  ["twitter.com", "x.com"]),
    (SourceType.WECHAT_OFFICIAL_ACCOUNT,  ["mp.weixin.qq.com"]),
    # WeCom / WeChat Channels share links via several domains
    (SourceType.WECHAT_CHANNEL,           ["channels.weixin.qq.com",
                                           "finder.video.qq.com",
                                           "weixin.qq.com/sph"]),
]


def identify_platform(url: str) -> SourceType:
    """
    Return the SourceType for the given URL.
    Falls back to SourceType.OTHER if no rule matches.
    """
    try:
        parsed = urlparse(url)
        # netloc includes port; lower-case for case-insensitive matching
        domain = parsed.netloc.lower()
        path = parsed.path.lower()
        full = domain + path  # lets us match path-based patterns too

        for source_type, patterns in PLATFORM_RULES:
            if any(p in full for p in patterns):
                return source_type
    except Exception as exc:
        logger.warning(f"Could not parse URL '{url}': {exc}")

    return SourceType.OTHER


# ---------------------------------------------------------------------------
# URL normalisation  (extension point for Step 2)
# ---------------------------------------------------------------------------

# Query parameters that carry no semantic meaning and pollute dedup keys
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source",
}


def normalize_url(url: str) -> str:
    """
    Return a canonical form of the URL for deduplication purposes.

    Current behaviour:
    - Lowercase scheme and host
    - Remove common tracking query parameters
    - Strip trailing slash from path

    Extend this function in Step 2 to handle platform-specific rewrites
    (e.g. stripping WeChat share tokens, expanding youtu.be short links, etc.)
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
        new_query = urlencode(cleaned_qs, doseq=True)
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            new_query,
            "",  # drop fragment
        ))
        return normalized
    except Exception:
        return url  # if anything goes wrong, return original


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_links(text: str) -> List[ExtractedLink]:
    """
    Extract all URLs from *text* and return them as ExtractedLink objects.

    Deduplication at this stage is based on the raw URL string; Notion-level
    dedup uses the normalized URL hash (see notion_client.py).
    """
    urls = extract_urls(text)
    links: List[ExtractedLink] = []
    for url in urls:
        source_type = identify_platform(url)
        links.append(ExtractedLink(url=url, source_type=source_type))
        logger.debug(f"Parsed link: {url} → {source_type.value}")
    return links
