"""
Transcript analyzer — Phase 4. Calls the OpenAI Chat Completions API.

Why raw HTTP (requests) instead of the `openai` SDK:
  This project pins pydantic 1.8.2 (required by fastapi 0.68 for the SCF
  deployment) and current openai SDKs require pydantic 2.x. The API is a
  simple JSON POST, so we call it directly.

Structured output: response_format json_schema with strict=true makes the
model return schema-valid JSON — no brittle "find the JSON in the text"
parsing. Requires every property in `required` and additionalProperties=false.

Model: settings.OPENAI_MODEL (default gpt-4o-mini, ~$0.002 per typical
20-minute video transcript).
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Cap what we send: ~150K chars ≈ 40K tokens. gpt-4o-mini's 128K-token context
# handles this fine; covers 2-3 hour videos. Longer transcripts get truncated
# with a note so the model knows the tail is missing.
MAX_TRANSCRIPT_CHARS = 150_000

REQUEST_TIMEOUT_SECONDS = 300

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
            "description": "The video's title if stated, otherwise a concise descriptive title inferred from the content",
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
    Run the OpenAI analysis over a transcript. Never raises.

    Args:
        transcript_text: Full plain-text transcript.
        video_title: Real title from oEmbed, if known — passed as context.
        channel: Channel name from oEmbed, if known.
    """
    if not settings.OPENAI_API_KEY:
        return AnalysisResult(success=False, error="OPENAI_API_KEY not set")

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
        "model": settings.OPENAI_MODEL,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{context}Analyze this video transcript:\n\n{truncated}{truncation_note}",
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "video_analysis",
                "strict": True,
                "schema": ANALYSIS_SCHEMA,
            },
        },
    }

    logger.info(f"[analyzer] Sending {len(truncated)} chars to {settings.OPENAI_MODEL}...")
    try:
        resp = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return AnalysisResult(success=False, error=f"OpenAI API unreachable: {exc}")

    if not resp.ok:
        # Log the body — OpenAI error messages name the exact problem
        logger.error(f"[analyzer] OpenAI API {resp.status_code}: {resp.text[:600]}")
        return AnalysisResult(success=False, error=f"OpenAI API {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})

    if message.get("refusal"):
        return AnalysisResult(success=False, error=f"Model refused: {message['refusal'][:200]}")

    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        logger.warning("[analyzer] Hit max_tokens — output may be truncated/unparseable")
    elif finish_reason == "content_filter":
        return AnalysisResult(success=False, error="Blocked by OpenAI content filter")

    text = message.get("content") or ""
    if not text:
        return AnalysisResult(success=False, error=f"Empty response (finish_reason={finish_reason})")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return AnalysisResult(success=False, error=f"Response JSON parse failed: {exc}")

    usage = data.get("usage", {})
    logger.info(
        f"[analyzer] Done — in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} tokens"
    )

    return AnalysisResult(
        success=True,
        # Real title from oEmbed wins; the model's inferred title is the fallback
        title=video_title or parsed.get("title", "") or "Untitled",
        summary=parsed.get("summary", ""),
        key_points=parsed.get("key_points", []),
        companies=parsed.get("companies", []),
        technologies=parsed.get("technologies", []),
        people=parsed.get("people", []),
        topics=parsed.get("topics", []),
    )
