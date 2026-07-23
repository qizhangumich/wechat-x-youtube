"""
Transcript analyzer — Phase 4. Calls the Claude Messages API.

Why raw HTTP (requests) instead of the official `anthropic` SDK:
  This project pins pydantic 1.8.2 (required by fastapi 0.68 for the SCF
  deployment) and the anthropic SDK requires pydantic 2.x. Rather than fight
  the dependency wall, we call POST /v1/messages directly — the request is a
  simple JSON POST and structured outputs give us schema-validated JSON back.

Model: claude-opus-4-8 with adaptive thinking. Structured outputs
(output_config.format json_schema) guarantee the response parses — no
brittle "find the JSON in the text" logic.

Cost note: a typical 20-minute video transcript (~8K tokens in, ~1K out)
costs roughly $0.05-0.07 per analysis at Opus pricing. Not worth optimizing
at personal scale.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-opus-4-8"

# Cap what we send: ~150K chars ≈ 40-50K tokens ≈ $0.25 per analysis worst
# case. Covers 2-3 hour videos; longer transcripts get truncated with a note.
MAX_TRANSCRIPT_CHARS = 150_000

# Claude may take a while on long transcripts (adaptive thinking + large input).
REQUEST_TIMEOUT_SECONDS = 600

SYSTEM_PROMPT = (
    "You are an analyst building a personal knowledge base from YouTube video "
    "transcripts. Given a transcript, produce a faithful, information-dense "
    "analysis. Extract only what is actually said or clearly implied — never "
    "invent companies, people, or facts that are not in the transcript. Write "
    "the summary and key points in the same language as the transcript "
    "(Chinese transcript → Chinese summary, English → English). Keep entity "
    "names (companies, technologies, people) in their original form."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "The video's title if stated, otherwise a concise descriptive title you infer from the content",
        },
        "summary": {
            "type": "string",
            "description": "A 2-4 sentence TL;DR of what the video covers and its main takeaway",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-10 bullet points capturing the key content structure and arguments, in order",
        },
        "companies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Company / organization / product-maker names mentioned substantively (not passing references)",
        },
        "technologies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Technologies, tools, frameworks, models, or products discussed",
        },
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "People in or discussed by the video, each with their role in parentheses, e.g. 'Jane Doe (interviewer)', 'John Smith (CEO of Acme, guest)'",
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-8 thematic tags for categorization, e.g. 'AI agents', 'venture capital', 'prompt engineering'",
        },
    },
    "required": ["title", "summary", "key_points", "companies", "technologies", "people", "topics"],
    "additionalProperties": False,
}


@dataclass
class AnalysisResult:
    success: bool
    title: str = ""
    summary: str = ""
    key_points: List[str] = field(default_factory=list)
    companies: List[str] = field(default_factory=list)
    technologies: List[str] = field(default_factory=list)
    people: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    error: Optional[str] = None


def analyze_transcript(transcript_text: str, video_title: Optional[str] = None,
                       channel: Optional[str] = None) -> AnalysisResult:
    """
    Run the Claude analysis over a transcript. Never raises.

    Args:
        transcript_text: Full plain-text transcript.
        video_title: Real title from oEmbed, if known — passed as context.
        channel: Channel name from oEmbed, if known.
    """
    if not settings.ANTHROPIC_API_KEY:
        return AnalysisResult(success=False, error="ANTHROPIC_API_KEY not set")

    truncated = transcript_text[:MAX_TRANSCRIPT_CHARS]
    truncation_note = ""
    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        truncation_note = "\n\n[NOTE: transcript truncated for length — analyze what is present]"
        logger.info(f"[analyzer] Transcript truncated {len(transcript_text)} → {MAX_TRANSCRIPT_CHARS} chars")

    context_lines = []
    if video_title:
        context_lines.append(f"Video title: {video_title}")
    if channel:
        context_lines.append(f"Channel: {channel}")
    context = ("\n".join(context_lines) + "\n\n") if context_lines else ""

    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "system": SYSTEM_PROMPT,
        "thinking": {"type": "adaptive"},
        # Routine extraction — medium effort balances quality and token spend
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
        "messages": [
            {
                "role": "user",
                "content": f"{context}Analyze this video transcript:\n\n{truncated}{truncation_note}",
            }
        ],
    }

    logger.info(f"[analyzer] Sending {len(truncated)} chars to {MODEL}...")
    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return AnalysisResult(success=False, error=f"Anthropic API unreachable: {exc}")

    if not resp.ok:
        # Log the body — Anthropic error messages are precise about what's wrong
        logger.error(f"[analyzer] Anthropic API {resp.status_code}: {resp.text[:600]}")
        return AnalysisResult(success=False, error=f"Anthropic API {resp.status_code}: {resp.text[:300]}")

    data = resp.json()

    stop_reason = data.get("stop_reason")
    if stop_reason == "refusal":
        return AnalysisResult(success=False, error="Model declined to analyze this content (refusal)")
    if stop_reason == "max_tokens":
        logger.warning("[analyzer] Hit max_tokens — output may be truncated/unparseable")

    # With output_config.format, the first text block contains valid JSON
    text = next(
        (b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"),
        "",
    )
    if not text:
        return AnalysisResult(success=False, error=f"No text in response (stop_reason={stop_reason})")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return AnalysisResult(success=False, error=f"Response JSON parse failed: {exc}")

    usage = data.get("usage", {})
    logger.info(
        f"[analyzer] Done — in={usage.get('input_tokens')} out={usage.get('output_tokens')} tokens"
    )

    return AnalysisResult(
        success=True,
        # Real title from oEmbed wins; Claude's inferred title is the fallback
        title=video_title or parsed.get("title", "") or "Untitled",
        summary=parsed.get("summary", ""),
        key_points=parsed.get("key_points", []),
        companies=parsed.get("companies", []),
        technologies=parsed.get("technologies", []),
        people=parsed.get("people", []),
        topics=parsed.get("topics", []),
    )
