# WeCom → Notion Link Collector

Automatically receives messages forwarded to WeCom (Enterprise WeChat),
extracts URLs, identifies their source platform, and saves them to a
Notion database as a content inbox.

**Current scope: Step 1 — capture only.**
Link unlocking, content extraction, and LLM summarisation are not yet implemented.

---

## Project Structure

```
wechat_collection/
├── main.py                      # FastAPI app + startup
├── config.py                    # All env-var configuration
├── models/
│   └── schemas.py               # Pydantic models shared across the app
├── routes/
│   └── wecom.py                 # WeCom callback + test endpoints
├── services/
│   ├── link_parser.py           # URL extraction & platform identification
│   ├── notion_client.py         # Notion API writes + dedup
│   └── message_service.py      # Pipeline orchestration
├── utils/
│   └── logger.py                # Centralised logging
├── requirements.txt
├── .env.example
└── README.md
```

---

## Prerequisites

- Python 3.10 or 3.11
- A Notion account with an integration token
- (Optional for full WeCom integration) A WeCom developer account

---

## Quick Start

### 1. Clone / download the project

```bash
cd D:\personal\ai_projects\56.wechat_collection
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# Windows (CMD)
.\.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
copy .env.example .env     # Windows
# cp .env.example .env     # macOS / Linux
```

Open `.env` and fill in at least:

| Variable             | Where to find it |
|----------------------|------------------|
| `NOTION_API_KEY`     | [notion.so/my-integrations](https://www.notion.so/my-integrations) → create an integration |
| `NOTION_DATABASE_ID` | Open your Notion database → share the page with your integration → copy the ID from the URL |

Leave the `WECOM_*` variables blank for now if you just want to test locally.

### 5. Set up the Notion database

Create a new Notion database (full page, not inline) with **exactly** these
property names and types:

| Property name   | Type      | Notes                        |
|-----------------|-----------|------------------------------|
| Title           | Title     | default name column          |
| Original URL    | URL       |                              |
| Source Type     | Select    | options auto-created on save |
| Platform        | Text      |                              |
| Captured From   | Select    |                              |
| Raw Message     | Text      |                              |
| Status          | Select    | default option: Inbox        |
| Created At      | Date      |                              |
| Dedup Key       | Text      |                              |
| Notes           | Text      |                              |

> **Important**: Share the database page with your Notion integration
> (click Share → Invite → select your integration).

### 6. Start the server

```bash
python main.py
```

Or using uvicorn directly:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

You should see:

```
Starting WeCom Collector (env=development)
Configuration OK
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## Testing Locally (No WeCom Account Needed)

### Health check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "ok", "env": "development", "notion_configured": true, "wecom_token_set": false}
```

### End-to-end test — POST a message with links

```bash
curl -X POST http://localhost:8000/webhook/wecom/test \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"Worth saving: https://mp.weixin.qq.com/s/abc123 https://x.com/user/status/123 https://youtu.be/xxxx\"}"
```

Expected response:
```json
{
  "success": true,
  "links_found": 3,
  "links_saved": 3,
  "links_skipped": 0,
  "results": [
    {"url": "https://mp.weixin.qq.com/s/abc123", "source_type": "wechat_official_account", "status": "saved", "notion_page_id": "..."},
    {"url": "https://x.com/user/status/123",     "source_type": "twitter",                  "status": "saved", "notion_page_id": "..."},
    {"url": "https://youtu.be/xxxx",              "source_type": "youtube",                  "status": "saved", "notion_page_id": "..."}
  ],
  "message": "Processed 3 link(s): 3 saved, 0 skipped."
}
```

### Test with no links

```bash
curl -X POST http://localhost:8000/webhook/wecom/test \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"Just a regular message with no links\"}"
```

Expected:
```json
{"success": true, "links_found": 0, "links_saved": 0, ...}
```

### Test deduplication

Run the same POST twice — the second time all results should show `"status": "duplicate"`.

### Interactive API docs

Open your browser at: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## WeCom Integration (Full Setup)

Once you're ready to connect real WeCom:

1. Go to **WeCom Admin Panel** → Applications → your app → **Receive Messages**
2. Set callback URL to: `https://<your-public-domain>/webhook/wecom`
3. Enter a Token (copy the same value to `WECOM_TOKEN` in `.env`)
4. For MVP, use **Plain Text Mode** (no encryption) — simpler to verify
5. Click "Save" — WeCom will send a GET request to verify the URL

> For local development, use [ngrok](https://ngrok.com/) or
> [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
> to expose your local server publicly:
> ```bash
> ngrok http 8000
> ```
> Then use the ngrok HTTPS URL as your WeCom callback URL.

---

## Roadmap

| Step | Feature                            | Status      |
|------|------------------------------------|-------------|
| 1    | Capture links from WeCom → Notion  | ✅ Complete  |
| 2    | Link unlocking / public URL        | Planned     |
| 2    | Web page content extraction        | Planned     |
| 3    | LLM title + summary generation     | Planned     |
| 3    | Auto-tagging                       | Planned     |

Extension points are marked with comments in the source code.

---

## Troubleshooting

**`EnvironmentError: Missing required environment variables`**
→ Check that `.env` exists and contains `NOTION_API_KEY` and `NOTION_DATABASE_ID`.

**`HTTP 400` from Notion on save**
→ Verify that all database property names match exactly (case-sensitive) and
  that the integration has been shared with the database.

**Links extracted but `status: error`**
→ Check that your Notion integration has "Insert content" permission and that
  the database is shared with it.

**WeCom verification fails**
→ Ensure `WECOM_TOKEN` in `.env` matches exactly what you entered in WeCom console.
   For initial testing, set it to blank and use Plain Text Mode.
