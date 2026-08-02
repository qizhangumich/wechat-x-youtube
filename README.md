# wechat-x-youtube — Link Collector + YouTube Analyzer

Receives links forwarded via **Telegram** (or WeCom / iOS Shortcut), identifies
their source platform, and saves them to a Notion database. YouTube links
additionally get their **full transcript fetched and analyzed by an LLM**, with
results saved to a second Notion database.

Deployed on Hetzner (`157.180.115.88`, Docker container `wechat-x-youtube`).

---

## How it works

```
Telegram bot  ──┐
WeCom callback ─┼─→ extract URLs → identify platform → save to wechat-x-y-db
iOS Shortcut ───┘                                        (dedup by URL hash)
                                                              │
                                              YouTube links only
                                                              ▼
                                    fetch transcript (residential proxy)
                                                              ▼
                                    OpenAI gpt-4o-mini: summary + arguments
                                                              ▼
                                    yt-topics DB: one page per video
                                    (full transcript in the page BODY,
                                     under the "Transcript" heading)
```

- Failures (no transcript, LLM error, Notion error) are reported back to you
  on Telegram. Success is silent — the page just appears in Notion.
- Re-sharing a video does **not** re-analyze it (dedup on `Video URL`).
- `/dl <url>` (or `/dla` for audio-only) also downloads the media via
  yt-dlp/Cobalt onto the server and records the file path in Notion.

## Project Structure

```
wechat_collection/
├── main.py                      # FastAPI app + startup
├── config.py                    # All env-var configuration
├── models/schemas.py            # Pydantic models
├── routes/
│   ├── telegram.py              # Telegram bot webhook (main entry point)
│   ├── wecom.py                 # WeCom callback + test endpoints
│   └── collect.py               # iOS Shortcut endpoint
├── services/
│   ├── link_parser.py           # URL extraction & platform identification
│   ├── notion_client.py         # Main-DB writes + dedup
│   ├── message_service.py       # Pipeline orchestration
│   ├── transcript_service.py    # YouTube transcript fetch (proxied)
│   ├── analyzer.py              # OpenAI argument-mining analysis
│   ├── youtube_analysis.py      # Phase 4 orchestration + yt-topics writes
│   ├── downloader.py            # yt-dlp downloads (/dl command)
│   ├── cobalt_client.py         # Cobalt fallback for YouTube downloads
│   └── poller.py                # Polls Notion for Download Status=Requested
└── utils/
```

## Notion databases

### `wechat-x-y-db` — link inbox (every link, all platforms)

| Property        | Type   | Written by                          |
|-----------------|--------|-------------------------------------|
| Title           | Title  | save pipeline (the URL)             |
| Original URL    | URL    | save pipeline                       |
| Source Type     | Select | youtube / twitter / wechat_channel / wechat_official_account / other |
| Captured From   | Select | Telegram / WeCom                    |
| Raw Message     | Text   | save pipeline                       |
| Status          | Select | Inbox (default) / Reviewed / Archived (manual) |
| Created At      | Date   | save pipeline                       |
| Dedup Key       | Text   | MD5 of normalized URL               |
| Download Status | Select | /dl command & poller                |
| Local File      | Text   | downloader                          |
| File Size MB    | Number | downloader                          |
| Duration        | Text   | downloader                          |
| Downloaded At   | Date   | downloader                          |
| Download Error  | Text   | downloader                          |

Views: default table (all rows), **▶ YouTube queue** (youtube only — by design),
**🧠 Analyzed library**, **𝕏 Twitter**, **💬 WeChat**.

### `yt-topics` — one page per analyzed YouTube video

| Property     | Type         | Written by                       |
|--------------|--------------|----------------------------------|
| Title        | Title        | video title — channel            |
| Video URL    | URL          | dedup key for the analysis pipeline — do not delete |
| Summary      | Text         | analyzer                         |
| Key Points   | Text         | analyzer (claims list)           |
| Companies    | Multi-select | analyzer                         |
| Technologies | Multi-select | analyzer                         |
| People       | Text         | analyzer                         |
| Topics       | Multi-select | analyzer                         |
| Duration     | Text         | transcript length                |
| Analyzed At  | Date         | pipeline                         |
| My Notes     | Text         | **you** — never overwritten      |

Page **body**: "Arguments & Logic" (claim → evidence → reasoning per argument)
followed by the **full transcript**.

## Configuration

See [.env.example](.env.example) and [config.py](config.py). Key variables:

| Variable               | Purpose                                        |
|------------------------|------------------------------------------------|
| `NOTION_API_KEY`       | Notion integration token                       |
| `NOTION_DATABASE_ID`   | wechat-x-y-db ID                               |
| `NOTION_YOUTUBE_DB_ID` | yt-topics DB ID                                |
| `TELEGRAM_BOT_TOKEN`   | from @BotFather                                |
| `TELEGRAM_ALLOWED_USERS` | comma-separated numeric user IDs             |
| `OPENAI_API_KEY`       | transcript analysis (gpt-4o-mini, ~$0.002/video) |
| `PROXY_URL`            | residential proxy — required on data-center IPs for transcripts |
| `YOUTUBE_ANALYSIS_ENABLED` | master switch for Phase 4                  |

## Local development

```bash
python -m venv .venv && .\.venv\Scripts\Activate.ps1   # Windows
pip install -r requirements.txt
copy .env.example .env    # fill in values
python main.py
```

Test without any messaging platform:

```bash
curl -X POST http://localhost:8000/webhook/wecom/test \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"https://youtu.be/xxxx\"}"
```

Interactive API docs: http://localhost:8000/docs

## Deployment

```bash
ssh root@157.180.115.88
cd /root/wechat-x-youtube && git pull
docker build -t wechat-x-youtube . && docker rm -f wechat-x-youtube && \
  # re-run with the same flags as before (see guide.md)
```

More detail: [guide.md](guide.md), [workflow.md](workflow.md), [lessons.md](lessons.md).

## History

| Phase | Feature                                        |
|-------|------------------------------------------------|
| 1     | Capture links from WeCom → Notion              |
| 2     | Telegram bot as main ingress                   |
| 3     | Media downloads (/dl, yt-dlp + Cobalt, poller) |
| 4     | YouTube transcript fetch + LLM argument mining |
| 2026-08 | Schema cleanup: dropped unused columns from an abandoned "queue/topic-map" design; per-platform views added |
