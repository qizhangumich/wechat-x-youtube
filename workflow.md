# Workflow — How a link travels from your phone to Notion

This document is the **runtime view** of the system: what happens when you share a link, step by step, across both ingress paths.

For one-time setup (env vars, ICP filing, deploys), see `guide.md`.
For the post-mortem of every wrong turn, see `lessons.md`.

---

## The big picture

```
┌─────────────────────────────────────┐         ┌─────────────────────────────────────┐
│  PHASE 1  —  Chinese platforms      │         │  PHASE 2  —  Global platforms       │
│                                     │         │                                     │
│  WeChat Channels video              │         │  X (Twitter) link                   │
│  WeChat Public Account article      │         │  YouTube video                      │
│           │                         │         │           │                         │
│           ▼                         │         │           ▼                         │
│  Personal WeChat → 分享 → 企业微信    │         │  Telegram chat with @bot          │
│           │                         │         │           │                         │
│           ▼                         │         │           ▼                         │
│  Tencent SCF (Guangzhou)            │         │  Fly.io (Singapore)                 │
│  wecom.tianrenyuan.com              │         │  wechat-x-youtube.fly.dev           │
│  /webhook/wecom                     │         │  /webhook/telegram                  │
└──────────────────┬──────────────────┘         └──────────────────┬──────────────────┘
                   │                                                │
                   │            SAME APPLICATION CODE               │
                   │     (services/message_service.py pipeline)     │
                   ▼                                                ▼
                   ┌──────────────────────────────────────────┐
                   │  parse_links() → extract URLs            │
                   │  link_parser  → tag with SourceType      │
                   │                  (YOUTUBE / TWITTER /    │
                   │                   WECHAT_CHANNEL / …)    │
                   │  notion_client → dedup check             │
                   │                → POST /v1/pages          │
                   └─────────────────────┬────────────────────┘
                                         │
                                         ▼
                              ┌────────────────────┐
                              │   Notion database  │
                              │   (one shared DB)  │
                              └────────────────────┘
```

Why two ingress hosts? Because **api.telegram.org is unreachable from mainland-China clouds** (GFW). Tencent SCF can talk to WeCom and Notion fine, but cannot reach Telegram. We deploy the same codebase to Fly Singapore for the Telegram path — same Notion database, same pipeline, different door.

---

## Phase 1 — WeChat Channels share

### Step-by-step

| # | Where | What happens | Concrete data |
|---|-------|--------------|---------------|
| 1 | iPhone, personal WeChat | You watch a Channels video, tap `⋯` → 分享给朋友 → 企业微信 | Video URL: `https://channels.weixin.qq.com/...` |
| 2 | 企业微信 (WeCom) on iPhone | Pick the bot conversation → confirm send | WeCom packages the share as a **link card** (MsgType=link), not plain text |
| 3 | WeCom backend | POST to your callback URL | `https://wecom.tianrenyuan.com/webhook/wecom?msg_signature=…&timestamp=…&nonce=…` with AES-encrypted XML body |
| 4 | SCF (Guangzhou) | `routes/wecom.py` verifies signature, AES-decrypts the envelope | Decrypted XML contains `<MsgType>link</MsgType><Title>…</Title><Url>…</Url><Description>…</Description>` |
| 5 | SCF | `_extract_text_from_xml()` synthesises a text body from the link card's three fields | `"<title>\n<description>\n<url>"` |
| 6 | SCF → shared pipeline | Hands off to `process_message(text)` | (see "Shared pipeline" below) |
| 7 | SCF | Returns `"success"` (always — even on errors) so WeCom doesn't retry forever | HTTP 200 with body `success` |

### What you see on the WeCom side

The bot does NOT reply in the chat after a Phase 1 save. Confirmation lives only in the SCF logs. (The reply path could be added — `WeCom Send Message API` — but it would face the same IP-whitelist problem we hit during seeding.)

---

## Phase 2 — Telegram share (X / YouTube / anything with a URL)

### Step-by-step

| # | Where | What happens | Concrete data |
|---|-------|--------------|---------------|
| 1 | Your phone or browser | Copy or share a YouTube/X link to the bot | `https://www.youtube.com/watch?v=…` or `https://x.com/user/status/…` |
| 2 | Telegram client | Sends the message to Telegram's servers | Standard Telegram Bot API protocol |
| 3 | Telegram backend | POST to the registered webhook | `https://wechat-x-youtube.fly.dev/webhook/telegram` with JSON body |
| 4 | Fly.io (Singapore) | `routes/telegram.py` checks the sender against `TELEGRAM_ALLOWED_USERS` | If not allowed → reply `⛔ You are not authorised`, return 200 |
| 5 | Fly | `_get_text_from_update()` extracts text + caption + URL entities | Combined string e.g. `"Look at this https://youtu.be/abc"` |
| 6 | Fly → shared pipeline | Hands off to `process_message(text)` | (see "Shared pipeline" below) |
| 7 | Fly | Calls Telegram `sendMessage` API with formatted summary | `✅ Found 1 link(s):` … `Saved: 1 \| Duplicates: 0 \| Errors: 0` |

### What you see on the Telegram side

The bot replies in the chat within ~2 seconds with a per-link breakdown. Useful for instant confirmation that the save worked.

---

## Shared pipeline (after both paths converge)

This is `services/message_service.py::process_message()`. Identical for both ingress paths.

```
text (raw message body)
   │
   ▼
┌──────────────────────────────────────────┐
│  parse_links(text)                       │
│  • Regex-extract every http(s):// URL    │
│  • Normalise (strip tracking params)     │
│  • Classify each by hostname:            │
│     youtube.com / youtu.be → YOUTUBE     │
│     twitter.com / x.com    → TWITTER     │
│     mp.weixin.qq.com       → WECHAT_OFFICIAL_ACCOUNT │
│     channels.weixin.qq.com → WECHAT_CHANNEL          │
│     anything else          → OTHER       │
│  Returns: List[ExtractedLink]            │
└──────────────────┬───────────────────────┘
                   │
                   ▼ (zero links → return early with links_found=0)
                   │
┌──────────────────────────────────────────┐
│  for each link:                          │
│    notion_client.save_link(link, raw)    │
│      │                                   │
│      ├── build_dedup_key(url) = MD5      │
│      │                                   │
│      ├── is_duplicate? — query DB by     │
│      │   "Dedup Key" property            │
│      │     yes → status="duplicate"      │
│      │     no  → continue                │
│      │                                   │
│      └── POST /v1/pages with full        │
│          property payload                │
│            success → status="saved"      │
│            HTTPError → status="error"    │
└──────────────────┬───────────────────────┘
                   │
                   ▼
            ProcessResult
            { links_found: N,
              links_saved: M,
              results: [LinkSaveResult, …] }
```

---

## Notion database schema (target)

The pipeline writes these properties on every save. Column names are case- AND space-sensitive — they must match exactly. Defined as constants at the top of `services/notion_client.py`.

| Property name | Type | Source | Example |
|---|---|---|---|
| `Title` | Title | The URL itself | `https://www.youtube.com/watch?v=dQw4w9WgXcQ` |
| `Original URL` | URL | Same as Title | (same) |
| `Source Type` | Select | Computed from hostname | `youtube`, `twitter`, `wechat_channel`, `wechat_official_account`, `other` |
| `Platform` | Rich text | Human-readable label of Source Type | `YouTube`, `Twitter / X`, `WeChat Channels`, … |
| `Captured From` | Select | **Currently hardcoded to `WeCom`** ⚠️ should be set to `Telegram` for Phase 2 (TODO) | `WeCom` |
| `Raw Message` | Rich text | The original message body (truncated to 2000 chars) | Whatever you sent |
| `Status` | Select | Hardcoded `Inbox` (workflow stage) | `Inbox` |
| `Created At` | Date | UTC timestamp at save | `2026-04-28T01:34:12+00:00` |
| `Dedup Key` | Rich text | MD5 of normalised URL | `9c1b2…` |
| `Notes` | Rich text | Empty (reserved for Phase 3 enrichment) | `""` |

---

## Where each component runs

| Component | Host | Region | Why there |
|---|---|---|---|
| WeCom callback | Tencent SCF | Guangzhou (`ap-guangzhou`) | WeCom requires ICP-filed `.cn`-resolvable domain |
| Telegram webhook | Fly.io | Singapore (`sin`) | api.telegram.org reachable from outside GFW |
| Notion API target | api.notion.com | Notion-managed (US) | Reachable from both SCF and Fly |
| `seed_local.py` (one-time) | Your laptop | Wherever you are | WeCom IP whitelist + SCF rotates IPs |

---

## Failure modes and how to spot them

| Symptom | Likely cause | Where to look |
|---|---|---|
| Telegram bot doesn't reply at all | Webhook not registered, or Fly app crashed | `curl …/webhook/telegram/info`; `fly logs` |
| Telegram bot replies "⛔ You are not authorised" | `TELEGRAM_ALLOWED_USERS` doesn't match sender | `fly secrets list`; verify your ID via `@userinfobot` |
| Bot replies `Saved: 1` but no Notion row | Integration not invited to the DB (Notion returns 404 on no-permission) | `fly logs` for `[notion] HTTP error 404` |
| Bot replies `Errors: 1` | Schema drift — DB property name changed, or Source Type select option missing | `fly logs` for full Notion error body |
| WeCom share to bot — nothing in Notion | Either signature mismatch or unhandled MsgType | SCF Custom logs (clear all filters!) for `Inbound XML (decrypted)` |
| All saves marked "Duplicate" | Dedup table polluted from earlier tests; URL was already saved | Search Notion DB by URL; delete the old row to retest |

---

## Future phases (currently not built)

### Phase 2.5 — multi-user routing
Right now `TELEGRAM_ALLOWED_USERS` is a single allow-list and every save lands in the same Notion DB. To open the bot to others without leaking your DB:
- Add a per-user `notion_database_id` mapping (small table or Notion DB)
- On each save, look up the sender's DB rather than using the env-var default
- New users `/start` → bot replies with onboarding link

### Phase 3 — enrichment / AI summary
The `Notes` property and `_process_link()` already have hooks for this:
- After a successful save, enqueue `(notion_page_id, url)` to a worker
- Worker fetches the URL, runs an LLM summary, calls Notion `update_page()`
- Could also extract: thumbnail, author, transcript (YouTube), etc.

The pipeline is intentionally synchronous today (simpler, easier to debug). Phase 3 would split save vs enrich into two stages.
