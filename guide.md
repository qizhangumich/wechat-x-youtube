# WeCom → Notion Link Collector — Complete Workflow Guide

End-to-end documentation of how a link shared in WeCom (Enterprise WeChat) becomes a row in a Notion database.

---

## 1. Architecture at a glance

```
┌──────────────┐    HTTPS POST    ┌────────────────────┐    HTTPS POST   ┌─────────────┐
│ WeCom client │ ───────────────▶ │ wecom.tianrenyuan  │ ──────────────▶ │  Notion API │
│  (chat/group)│   encrypted XML  │   .com (SCF)       │   JSON payload  │             │
└──────────────┘                  │   FastAPI app      │                 └─────────────┘
                                  └────────────────────┘
                                           │
                                           ▼
                                  ┌─────────────────────┐
                                  │ Decrypt → extract   │
                                  │ URLs → dedup → save │
                                  └─────────────────────┘
```

| Component | Tech | Hostname / ID |
|---|---|---|
| Edge | Tencent Cloud SCF (Serverless Cloud Function), Web Function mode | `wecom-collector` (region: ap-guangzhou) |
| Custom domain | ICP-filed `.com` + SCF Custom Domain + Tencent free DV cert | `wecom.tianrenyuan.com` |
| App | FastAPI on Python 3.10, uvicorn on port 9000 | `main:app` |
| Encryption | AES-256-CBC (pycryptodome) | EncodingAESKey from WeCom console |
| Storage | Notion database via REST API v1 | DB ID `34f9305042de8003a2e7c097f51c4aba` |

---

## 2. One-time setup checklist

Before any message can flow, these must all be true:

| # | Item | How to verify |
|---|---|---|
| 1 | ICP filing approved for `tianrenyuan.com` under your company entity | 阿里云 备案管理 console shows 已备案 |
| 2 | DNS CNAME `wecom.tianrenyuan.com → 1255478305.ap-guangzhou.tencentscf.com` | `nslookup wecom.tianrenyuan.com` returns an IP |
| 3 | Free DV SSL cert issued and bound to the domain in SCF Custom Domain | `curl -I https://wecom.tianrenyuan.com/health` returns 200 |
| 4 | SCF function deployed with bundled Linux packages | `/health` returns `notion_configured: true` |
| 5 | SCF env vars set (5 vars listed in §3) | SCF console → 函数配置 → 环境变量 |
| 6 | WeCom Token + EncodingAESKey saved successfully (encrypted mode) | WeCom console shows 保存成功 |
| 7 | Notion integration created at notion.so/my-integrations | `NOTION_API_KEY` starts with `ntn_` or `secret_` |
| 8 | Notion database has 10 required columns (§7) | Visible in DB header |
| 9 | Notion integration invited to the database via Connections | Database **⋯** → Connections shows your integration |
| 10 | App's **Allowed Users** in WeCom admin backend includes you | https://work.weixin.qq.com → 应用管理 → wechat-x-youtube → Allowed users |
| 11 | Your **laptop's IPv4** whitelisted in WeCom 企业可信IP | Required only for the one-time seed; not for inbound callbacks |
| 12 | Seed script run once to create the bot conversation in your chat list | `python seed_local.py` returns `errcode: 0` |

---

## 3. Required environment variables

Set in **SCF console** → wecom-collector → 函数配置 → 环境变量. (Local `.env` is for development only — SCF doesn't read it.)

| Variable | Example value | Where it comes from |
|---|---|---|
| `NOTION_API_KEY` | `ntn_394652720302UWXug...` | https://www.notion.so/my-integrations |
| `NOTION_DATABASE_ID` | `34f9305042de8003a2e7c097f51c4aba` | Notion DB URL: `notion.so/<workspace>/<DB_ID>?v=...` |
| `WECOM_TOKEN` | `2qw6kMVov5ITInT8U2U5kKetzlat` | WeCom dev console → 接收消息 → Token |
| `WECOM_ENCODING_AES_KEY` | `k54ia9jPdDONEkQahc9Eh0OAQfCeHZfcA8eXCJ3l1a9` | WeCom dev console → 接收消息 → EncodingAESKey (43 chars) |
| `WECOM_CORP_ID` | `wwc58f0762e5e753ec` | WeCom Admin → 我的企业 → 企业信息 → 企业ID (NO trailing space) |
| `WECOM_CORP_SECRET` | `g92R4KseP9m_OEtTsK4x5M7-14-T_NPQBJWl0nRiirw` | App settings → Secret → View. Used for outbound API calls (sending hello messages back to users) |
| `WECOM_AGENT_ID` | `1000002` | App settings → AgentId. The unique ID of your self-built app |

---

## 4. End-to-end data flow

This section walks through one real message, showing **input** and **output** at every boundary.

### Step 4.1 — WeCom client sends the message

**User action:** In WeCom (1-on-1 chat with the corp app, or @-mention in a group):

```
Check this out https://mp.weixin.qq.com/s/abc123
```

**WeCom server's HTTPS POST:**

- **URL:** `https://wecom.tianrenyuan.com/webhook/wecom?msg_signature=xxx&timestamp=1777254928&nonce=12345`
- **Method:** POST
- **Body (XML, encrypted):**

```xml
<xml>
  <ToUserName><![CDATA[wwc58f0762e5e753ec]]></ToUserName>
  <Encrypt><![CDATA[abcDEF...base64...XYZ==]]></Encrypt>
  <AgentID><![CDATA[1000002]]></AgentID>
</xml>
```

---

### Step 4.2 — Tencent SCF routes the request to FastAPI

**Input:** HTTPS request hits `wecom.tianrenyuan.com` → CNAME → SCF Function URL → `scf_bootstrap` → uvicorn → FastAPI router matches `POST /webhook/wecom`.

**No transformation here** — SCF is a transparent HTTP proxy.

---

### Step 4.3 — Signature verification

**Code:** `routes/wecom.py` → `wecom_callback()` → `verify_signature()` (from `utils/wecom_crypto.py`)

**Input:**
- `token = "2qw6kMVov5ITInT8U2U5kKetzlat"`
- `timestamp = "1777254928"`
- `nonce = "12345"`
- `encrypted = "abcDEF...base64...XYZ=="` (extracted from `<Encrypt>` tag)
- `msg_signature = "xxx"` (from query param)

**Algorithm:**
1. Sort `[token, timestamp, nonce, encrypted]` alphabetically
2. Concatenate
3. SHA1, hex digest

**Output:** boolean.
- ✅ Match → proceed to decrypt
- ❌ Mismatch → log warning, return `"success"` (WeCom retries forever on errors, so we always 200)

---

### Step 4.4 — AES-256-CBC decryption

**Code:** `utils/wecom_crypto.py` → `decrypt()`

**Input:**
- `encoding_aes_key = "k54ia9jPdDONEkQahc9Eh0OAQfCeHZfcA8eXCJ3l1a9"`
- `encrypted_b64 = "abcDEF...XYZ=="`
- `expected_receive_id = "wwc58f0762e5e753ec"` (your CorpID)

**Algorithm:**
1. AES key = `base64.b64decode(encoding_aes_key + "=")` → 32 bytes
2. IV = first 16 bytes of AES key
3. AES-256-CBC decrypt
4. PKCS#7 unpad
5. Skip 16 random bytes
6. Read 4-byte big-endian length
7. Extract message of that length
8. Verify trailing bytes == expected CorpID

**Output:** plaintext XML string:

```xml
<xml>
  <ToUserName><![CDATA[wwc58f0762e5e753ec]]></ToUserName>
  <FromUserName><![CDATA[OliviaLi]]></FromUserName>
  <CreateTime>1777254928</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[Check this out https://mp.weixin.qq.com/s/abc123]]></Content>
  <MsgId>7234567890123456789</MsgId>
  <AgentID>1000002</AgentID>
</xml>
```

---

### Step 4.5 — XML parse → text content

**Code:** `routes/wecom.py` → `_extract_text_from_xml()`

**Input:** plaintext XML bytes

**Diagnostic:** First, the full decrypted XML is logged at INFO level (`Inbound XML (decrypted): ...`) so you can inspect any unfamiliar `MsgType` in SCF logs.

**Two supported MsgType values:**

| MsgType | Source | Output |
|---|---|---|
| `text` | Direct typed/pasted message | Content of `<Content>` |
| `link` | Forwarded post / shared article / **WeChat Channels video shared from personal WeChat** | Synthetic string: `"<Url> [Title] <Title> [Desc] <Description>"` so URL extraction + Notion's Raw Message column both get rich data |

**Output for the example:** `"Check this out https://mp.weixin.qq.com/s/abc123"`

**Returns None** for image, voice, video, miniprogram, event, etc. Those are acknowledged with `"success"` but skipped — see SCF logs for the exact `MsgType` if you want to extend support.

---

### Step 4.6 — URL extraction

**Code:** `services/link_parser.py` → `extract_urls()` → `parse_links()`

**Input:** `"Check this out https://mp.weixin.qq.com/s/abc123"`

**Regex:** Matches `https?://` followed by non-whitespace, non-CJK-punctuation chars, stops before trailing `.,;:!?)`.

**Output:**
```python
[
  ExtractedLink(
    url="https://mp.weixin.qq.com/s/abc123",
    source_type=SourceType.WECHAT_OFFICIAL_ACCOUNT,
  )
]
```

**Source type rules** (`PLATFORM_RULES` in `link_parser.py`):

| Platform | Domain patterns |
|---|---|
| YouTube | `youtube.com`, `youtu.be` |
| Twitter / X | `twitter.com`, `x.com` |
| WeChat Official Account | `mp.weixin.qq.com` |
| WeChat Channels | `channels.weixin.qq.com`, `finder.video.qq.com`, `weixin.qq.com/sph` |
| Other | (anything else) |

---

### Step 4.7 — Dedup check

**Code:** `services/notion_client.py` → `is_duplicate()`

**Input:** `url = "https://mp.weixin.qq.com/s/abc123"`

**Algorithm:**
1. Normalize URL: lowercase scheme/host, strip tracking params (`utm_*`, `fbclid`, etc.), drop trailing slash, drop fragment
2. MD5 hash → `dedup_key` (32-char hex)
3. Query Notion DB for any row with `Dedup Key == dedup_key`

**Output:** boolean.
- ✅ Found → return `LinkSaveResult(status="duplicate")`, skip create
- ❌ Not found → proceed to create

---

### Step 4.8 — Notion page creation

**Code:** `services/notion_client.py` → `create_page()`

**Input:** `ExtractedLink` + raw message text

**HTTP request:**
- **POST** `https://api.notion.com/v1/pages`
- **Headers:**
  ```
  Authorization: Bearer ntn_394652720302UWXug...
  Notion-Version: 2022-06-28
  Content-Type: application/json
  ```
- **Body:**
  ```json
  {
    "parent": {"database_id": "34f9305042de8003a2e7c097f51c4aba"},
    "properties": {
      "Title":          {"title":     [{"text": {"content": "https://mp.weixin.qq.com/s/abc123"}}]},
      "Original URL":   {"url":       "https://mp.weixin.qq.com/s/abc123"},
      "Source Type":    {"select":    {"name": "wechat_official_account"}},
      "Platform":       {"rich_text": [{"text": {"content": "WeChat Official Account"}}]},
      "Captured From":  {"select":    {"name": "WeCom"}},
      "Raw Message":    {"rich_text": [{"text": {"content": "Check this out https://mp.weixin.qq.com/s/abc123"}}]},
      "Status":         {"select":    {"name": "Inbox"}},
      "Created At":     {"date":      {"start": "2026-04-27T02:30:00+00:00"}},
      "Dedup Key":      {"rich_text": [{"text": {"content": "9b4e7c2a..."}}]},
      "Notes":          {"rich_text": []}
    }
  }
  ```

**Output (Notion response):**
```json
{
  "object": "page",
  "id": "34f93050-42de-8192-a7d1-d0de9f61c652",
  "created_time": "2026-04-27T02:30:00.000Z",
  "url": "https://www.notion.so/...",
  ...
}
```

---

### Step 4.9 — Aggregate result

**Code:** `services/message_service.py` → `process_message()`

**Output (`ProcessResult`):**
```json
{
  "success": true,
  "links_found": 1,
  "links_saved": 1,
  "links_skipped": 0,
  "results": [
    {
      "url": "https://mp.weixin.qq.com/s/abc123",
      "source_type": "wechat_official_account",
      "status": "saved",
      "notion_page_id": "34f93050-42de-8192-a7d1-d0de9f61c652",
      "error": null
    }
  ],
  "message": "Processed 1 link(s): 1 saved, 0 skipped."
}
```

---

### Step 4.10 — Response back to WeCom

**Code:** `routes/wecom.py` → returns the literal string `"success"` (HTTP 200).

WeCom requires exactly this body to consider the callback successful. Any other response triggers automatic retries (which would create duplicate Notion entries — that's why dedup matters).

---

## 5. URL verification flow (one-time, when saving callback URL)

This is what happened when you clicked **Save** in the WeCom console. WeCom needs to confirm your server holds the AES key.

| Step | Direction | Data |
|---|---|---|
| 1 | WeCom → server | `GET /webhook/wecom?msg_signature=...&timestamp=...&nonce=...&echostr=<encrypted>` |
| 2 | Server | Verify SHA1 of sorted `[token, ts, nonce, echostr]` matches `msg_signature` |
| 3 | Server | AES-decrypt `echostr` with EncodingAESKey, verify trailing receive_id == CorpID |
| 4 | Server → WeCom | Plain-text body: the decrypted echostr (just the inner message portion) |
| 5 | WeCom | Compare returned plaintext with what it originally encrypted → match → 保存成功 |

---

## 6. Local testing — bypass WeCom entirely

Use this for fast iteration without involving WeCom or AES.

### Endpoint

`POST /webhook/wecom/test` — no signature, no encryption.

### PowerShell

```powershell
Invoke-RestMethod -Uri "https://wecom.tianrenyuan.com/webhook/wecom/test" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"text": "Test https://mp.weixin.qq.com/s/abc123"}'
```

### curl

```bash
curl -X POST https://wecom.tianrenyuan.com/webhook/wecom/test \
  -H "Content-Type: application/json" \
  -d '{"text": "Test https://mp.weixin.qq.com/s/abc123"}'
```

### Expected output

```json
{
  "success": true,
  "links_found": 1,
  "links_saved": 1,
  "links_skipped": 0,
  "results": [{"url": "...", "status": "saved", "notion_page_id": "..."}],
  "message": "Processed 1 link(s): 1 saved, 0 skipped."
}
```

---

## 7. Notion database schema

Create a database with **exactly** these 10 columns (case-sensitive):

| # | Name | Type | Notes |
|---|---|---|---|
| 1 | `Title` | Title | The default Title column — rename from "Name" if needed |
| 2 | `Original URL` | URL | The captured link |
| 3 | `Source Type` | Select | Auto-creates options: `youtube`, `twitter`, `wechat_official_account`, `wechat_channel`, `other` |
| 4 | `Platform` | Text | Human-readable label (e.g. "WeChat Official Account") |
| 5 | `Captured From` | Select | Currently always `WeCom` |
| 6 | `Raw Message` | Text | Full original message text (truncated to 2000 chars) |
| 7 | `Status` | Select | Currently always `Inbox` (use for your own workflow) |
| 8 | `Created At` | Date | UTC timestamp set by the server |
| 9 | `Dedup Key` | Text | MD5 of normalized URL — DO NOT edit; used for dedup queries |
| 10 | `Notes` | Text | Empty for now — reserved for future enrichment (summaries, etc.) |

**Don't forget:** Database **⋯** → Connections → add your integration. Without this, every API call returns `404 object_not_found`.

---

## 8. Bootstrapping the bot conversation (one-time seed)

WeCom self-built apps don't automatically appear as direct chat contacts. The bot conversation has to be **seeded** by sending one message *from* the bot *to* you. After that, the conversation lives in your 消息 tab forever.

### Why this is needed

WeCom's UX assumption: self-built apps are "tools" that admins use to push notifications to employees. Two-way chat is supported, but the conversation thread only exists after the bot has spoken first.

### Why not seed from SCF?

WeCom's outbound API (qyapi.weixin.qq.com) requires the calling IP to be on the app's 企业可信IP whitelist. SCF rotates outbound IPs across hundreds of addresses, making it impractical to whitelist them all. So we run the seed from a **machine with a stable IPv4** — your laptop.

### One-time setup

1. **Find your laptop's public IPv4:**
   ```powershell
   Invoke-RestMethod https://api.ipify.org
   ```
   Should be something like `58.96.232.138`. (If it returns IPv6 — 8 hex groups separated by colons — see §10 troubleshooting.)

2. **Whitelist it in WeCom:**
   - https://work.weixin.qq.com → 应用管理 → wechat-x-youtube
   - Find **企业可信IP** section
   - Add your IPv4 → Save

3. **Run the seed script:**
   ```powershell
   python seed_local.py                       # default: send to @all
   python seed_local.py "Jeremy"              # one specific user
   python seed_local.py "Jeremy,zhangqi"      # multiple users
   python seed_local.py @all "Custom hello"   # custom message
   ```

4. **Expected output:**
   ```
   WeCom response: {'errcode': 0, 'errmsg': 'ok', 'msgid': '...'}
   OK Delivered to: @all
   ```

5. **Open WeCom** (mobile or desktop) → 消息 tab → bot conversation appears at the top.

After step 5, you never need to seed again unless you add new users.

### Finding UserIDs

The display name in WeCom desktop client is **not** necessarily the UserID. To find the actual UserID:

1. https://work.weixin.qq.com → **通讯录** (Address Book)
2. Click the member's row → side panel opens
3. Look for **账号** field — that's the UserID

Or just use `@all` to broadcast — WeCom resolves IDs server-side and skips unjoined users automatically.

### Important: 未加入 status

If a member shows **未加入** (Not Joined) in the address book, they haven't activated their WeCom account yet. They cannot receive messages. They'll appear in the seed response's `invaliduser` field. To fix: have the admin re-send the invitation, accept on phone, log in.

---

## 9. How to use the system day-to-day

Once setup is complete (sections 2–8), you can save links from three different surfaces. They all land in the **same Notion database**. Pick whichever path matches the source content:

| Source content | Recommended path | Goes through |
|---|---|---|
| WeChat Channels video, WeChat public-account article | **9.1** WeCom 1-on-1 with the bot | SCF (Guangzhou) |
| Multiple people in a WeCom group sharing links | **9.2** WeCom group with @-mention | SCF (Guangzhou) |
| X (Twitter) post, YouTube video, anything else with a URL | **9.3** Telegram bot | Fly (Singapore) |

---

### 9.1 — Save from personal WeChat / WeCom 1-on-1 chat

**The most common flow** — you found a video or article on personal WeChat and want it in Notion.

**Steps:**
1. In personal WeChat, open the video / article
2. Tap `⋯` (top right) → **分享** (Share)
3. Pick **企业微信** (WeCom) from the share targets
4. WeCom opens with a recipient picker → select **the bot's conversation** (the one seeded by `seed_local.py` — pinned at top of your 消息 tab)
5. Tap **发送** (Send)

**What you'll see:**
- The link appears as a card in the WeCom chat with the bot
- The bot **does not reply** in the chat (Phase 1 has no reply path — by design, to avoid the IP-whitelist problem we hit during seeding)
- Within ~2 seconds the link should appear as a new row in your Notion DB

**Verify it landed:**
- Open Notion → your collection database → newest row at the top
- `Captured From = WeCom`, `Source Type = wechat_channel` or `wechat_official_account`

**Troubleshooting:**
- Nothing in Notion after 30s? See §11 troubleshooting → "WeCom share didn't reach Notion"
- Bot conversation missing from your WeCom 消息 tab? Re-run `python seed_local.py` (see §8)

---

### 9.2 — Save from a WeCom group chat (with @-mention)

For when **multiple people** want to dump links into the same Notion DB. WeCom self-built apps don't passively read group messages — they only fire when **@-mentioned**.

**One-time group setup:**
1. Open the WeCom group → 设置 → 群成员管理 → 添加应用
2. Search for your app → add

**Sending a link:**
- In the group, type `@` → select your app from the picker → paste the link
- Format: `@AppName https://mp.weixin.qq.com/s/abc123`
- Same `wecom.tianrenyuan.com/webhook/wecom` endpoint handles it; the XML payload adds a `<ChatId>` field which the parser ignores

**Why the @-mention is required:** WeCom only delivers group messages to bots that were explicitly addressed. For passive group capture (no @-mention), you'd need WeCom 会话内容存档 (Conversation Content Archive) — a separate enterprise API, out of scope for this project.

---

### 9.3 — Save X / YouTube / general links via Telegram

For sources outside the WeChat ecosystem. Uses the Fly Singapore deployment because `api.telegram.org` is unreachable from mainland-China clouds.

**Steps:**
1. Open Telegram → search **`@wechatxyoutube_bot`** → start the chat
2. **Forward** any message containing a URL, OR **paste** the URL directly
3. The bot **replies within ~2 seconds** with a per-link breakdown:

   ```
   ✅ Found 1 link(s):
   💾 [Youtube] https://www.youtube.com/watch?v=...

   Saved: 1  |  Duplicates: 0  |  Errors: 0
   ```

4. The new row appears in the same Notion database

**Supported source types** (auto-detected from hostname):
- `youtube.com`, `youtu.be` → `Source Type = youtube`
- `twitter.com`, `x.com` → `Source Type = twitter`
- Anything else → `Source Type = other`

**Three reply patterns and what they mean:**
- `✅ Found 1 link(s) … Saved: 1` → success
- `🔁 Duplicate` → URL already exists in the DB (deduplicated by MD5 of normalised URL)
- `❌ Error` → Notion rejected the page. Run `fly logs` to see the API error body
- `⚠️ No links found in your message` → no `http(s)://` substring detected

**Lock-down:** the bot only replies to user IDs in `TELEGRAM_ALLOWED_USERS` (Fly secret). Anyone else gets `⛔ You are not authorised`. To find your numeric user ID, message `@userinfobot` on Telegram.

---

### 9.4 — Verifying a save lands correctly

Regardless of which path you used, expected Notion row contents:

| Property | Value |
|---|---|
| `Title` | The full URL (until enrichment in Phase 3) |
| `Original URL` | The URL again, as a clickable link |
| `Source Type` | One of `youtube`, `twitter`, `wechat_channel`, `wechat_official_account`, `other` |
| `Platform` | Human label: `YouTube`, `Twitter / X`, `WeChat Channels`, etc. |
| `Captured From` | `WeCom` (currently hardcoded — TODO: set `Telegram` for Phase 2 saves) |
| `Status` | `Inbox` |
| `Created At` | UTC timestamp |
| `Dedup Key` | MD5 hex (don't edit) |
| `Raw Message` | The full message body that came in |
| `Notes` | Empty (reserved for Phase 3 AI summary) |

If the row appears but a column is **blank**, the property name in Notion doesn't match what `notion_client.py` expects. Names are case- AND space-sensitive (Lesson D3) — see §7 for the exact spec.

---

### 9.5 — Tips for daily use

- **Pin the bot conversations** in both WeCom and Telegram so they sit at the top of your message lists
- **The dedup is per-URL after normalisation** — sharing the same Channels video twice (even with different referrer params) only creates one row
- **Status column is your inbox/triage workflow** — bot creates everything as `Inbox`. Move rows to `Reviewed` / `Archived` manually as you process them
- **Long-form messages with multiple URLs** all save in one shot — the parser extracts every `http(s)://` substring
- **No bot reply on WeCom side** is intentional, not a bug. Confirmation lives in Notion (Phase 1) or in the bot reply (Phase 2)

---

## 10. Deployment workflow

When you change code or add dependencies, redeploy with these PowerShell commands.

### 10.1 — Re-bundle Linux packages (only when `requirements.txt` changes)

```powershell
Remove-Item -Recurse -Force packages -ErrorAction SilentlyContinue

pip install --platform manylinux2014_x86_64 `
  --python-version 3.10 `
  --only-binary=:all: `
  --implementation cp `
  -t ./packages -r requirements.txt
```

Verify:
```powershell
Test-Path .\packages\Crypto\Cipher\AES.py    # → True (or whatever new dep you added)
```

### 10.2 — Rebuild deploy.zip (always required)

```powershell
Remove-Item deploy.zip -ErrorAction SilentlyContinue

python -c "import zipfile, os; z = zipfile.ZipFile('deploy.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'.')) for r,_,fs in os.walk('.') for f in fs if not any(p in r for p in ['.git','__pycache__','.venv','node_modules']) and f != 'deploy.zip']; z.close(); print('done — size:', os.path.getsize('deploy.zip'))"
```

> ⚠ Do **not** use PowerShell's `Compress-Archive` — SCF rejects its zip format.

### 10.3 — Verify zip contents

```powershell
python -c "import zipfile; z=zipfile.ZipFile('deploy.zip'); names=z.namelist(); [print('OK ', n) for n in ['main.py','routes/wecom.py','utils/wecom_crypto.py','packages/Crypto/Cipher/AES.py'] if n in names]"
```

All 4 lines should print.

### 10.4 — Upload to SCF

1. SCF console → wecom-collector → 函数代码
2. **上传 zip 包** → select `deploy.zip` → 部署
3. Wait for "部署成功" toast (~10s)

### 10.5 — Smoke test

```bash
curl -i "https://wecom.tianrenyuan.com/webhook/wecom?echostr=test123"
```

Expect: `400 Bad Request` with `"detail":"Missing msg_signature/timestamp/nonce — encrypted mode requires all three"`.

If you get `200 test123` instead → old code still running OR `WECOM_ENCODING_AES_KEY` env var missing.

---

## 11. Operations & troubleshooting

### Where to look when things break

| Symptom | First place to check |
|---|---|
| WeCom save fails | SCF logs — search by recent timestamp |
| Message sent in chat, nothing in Notion | SCF logs — search by RequestId from the timestamp of your message |
| `/health` returns 500 | SCF logs — look for `ImportError` (missing dep) |
| Notion API 404 | Connections panel — integration not invited to DB |
| Notion API "property does not exist" | DB column name typo |

### Reading SCF logs

SCF console → wecom-collector → **日志查询**.

- **Clear filters** — by default it may show only `SCF_Type:Platform` (request summaries, not your app logs)
- Search box: paste a RequestId, or use a time window
- Look for `INFO`/`WARNING`/`ERROR` lines from your `logger.*` calls

Key log lines to grep for:

| Log line | Meaning |
|---|---|
| `WeCom callback received — body length: ...` | Request reached FastAPI |
| `WeCom signature mismatch` | Token wrong, or query params tampered with |
| `echostr decryption failed: receive_id mismatch` | CorpID env var doesn't match WeCom's CorpID |
| `echostr decryption failed: Padding is incorrect` | EncodingAESKey wrong |
| `[notion] Saved: ... (page_id=...)` | Success |
| `[notion] Duplicate — skipped: ...` | URL already in DB |
| `[notion] HTTP error saving '...': 404` | Integration not invited, or wrong DB ID |

### Common errors and fixes

| Error | Fix |
|---|---|
| `Missing echostr parameter` (when curling `/webhook/wecom` directly) | Expected — WeCom provides this in real verification |
| `Domain entity verification failed` (in WeCom UI) | ICP filing entity doesn't match WeCom corp entity, OR WeCom's ICP cache hasn't refreshed (≤72h) |
| Notion 404 on `/v1/pages` | Integration not added to DB Connections |
| Notion 400 `validation_error: ... is not a property` | Column name mismatch — check `routes/wecom.py` constants vs. DB |
| SCF 500 with `libssl.so.10: cannot open shared object file` | Used `/var/lang/python3` (Python 3.6) instead of `/var/lang/python310/bin/python3.10` in `scf_bootstrap` |
| `pip install ... ERROR: No matching distribution found` | SCF can't reach PyPI — bundle packages locally instead (§10.1) |
| `errcode: 60020 not allow to access from your ip` (when calling seed) | Your laptop's IPv4 isn't in 企业可信IP whitelist. Find IP via `Invoke-RestMethod https://api.ipify.org`, add it. **Do not** add SCF IPs — they rotate |
| `api.ipify.org` returns IPv6 (8 hex groups) | WeCom whitelist only accepts IPv4. Switch network, use VPN with IPv4 exit, or tether through phone |
| `errcode: 60011 not allowed to access this user` | UserID not in app's Allowed Users list |
| `errcode: 0` but recipient shows in `invaliduser` | That user has 未加入 status — hasn't activated their WeCom account |
| Bot conversation doesn't appear in 消息 tab after seed | Wait 10 sec, force-quit and reopen WeCom mobile app |
| Channel video shared but nothing in Notion | Check SCF log for `Inbound XML (decrypted): ...` — if MsgType is something other than `text`/`link`, parser needs extension |

---

## 12. File map

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app entry, health check, router registration |
| `config.py` | Loads env vars into a `Settings` singleton |
| `routes/wecom.py` | WeCom GET (verify) + POST (callback) + POST `/test` |
| `routes/telegram.py` | Telegram bot webhook (alternative input channel) |
| `routes/collect.py` | iOS Shortcut endpoint |
| `services/message_service.py` | Pipeline orchestrator (text → links → Notion) |
| `services/link_parser.py` | URL regex + platform classification + normalization |
| `services/notion_client.py` | Notion REST API client (dedup + page create) |
| `models/schemas.py` | Pydantic models — `ExtractedLink`, `ProcessResult`, etc. |
| `utils/wecom_crypto.py` | AES-256-CBC decrypt/encrypt + signature for WeCom |
| `utils/logger.py` | Centralised logger factory |
| `scf_bootstrap` | Bash script SCF runs: starts uvicorn on port 9000 |
| `seed_local.py` | Run from your laptop (NOT SCF) to bootstrap the bot conversation. Uses your stable home/office IPv4 to bypass WeCom's IP whitelist on outbound calls |
| `requirements.txt` | Python deps: fastapi, uvicorn, requests, pydantic, python-dotenv, pycryptodome |
| `packages/` | Pre-bundled Linux wheels for SCF runtime |
| `deploy.zip` | What you upload to SCF |
| `.env` | Local-dev only — SCF uses console env vars. Used by `seed_local.py` |

---

## 13. Future extension points

Marked in the code with comments so you can find them later:

| Where | What |
|---|---|
| `services/link_parser.py` → `PLATFORM_RULES` | Add new source platforms (Bilibili, Reddit, etc.) |
| `services/link_parser.py` → `normalize_url()` | Platform-specific URL canonicalization (strip WeChat share tokens, expand youtu.be) |
| `services/message_service.py` → `_process_link()` | Hook for enrichment (LLM summary, title fetch) after save |
| `routes/wecom.py` → POST handler | Echo a confirmation message back into the chat (encrypt + sign) |
| `utils/wecom_crypto.py` → `encrypt()` | Already implemented — use for outbound messages |

---

## 14. Quick reference

**Test the live endpoint:**
```powershell
Invoke-RestMethod https://wecom.tianrenyuan.com/health
Invoke-RestMethod https://wecom.tianrenyuan.com/webhook/wecom/test -Method Post `
  -ContentType "application/json" -Body '{"text":"https://example.com"}'
```

**Redeploy:**
```powershell
# Only if requirements.txt changed:
Remove-Item -Recurse -Force packages; pip install --platform manylinux2014_x86_64 --python-version 3.10 --only-binary=:all: --implementation cp -t ./packages -r requirements.txt

# Always:
Remove-Item deploy.zip; python -c "import zipfile, os; z = zipfile.ZipFile('deploy.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'.')) for r,_,fs in os.walk('.') for f in fs if not any(p in r for p in ['.git','__pycache__','.venv','node_modules']) and f != 'deploy.zip']; z.close()"

# Then upload deploy.zip via SCF console.
```

**Local seed (run once after adding new users):**
```powershell
# 1. Find your laptop IPv4
Invoke-RestMethod https://api.ipify.org

# 2. Whitelist it at: work.weixin.qq.com → 应用管理 → wechat-x-youtube → 企业可信IP

# 3. Send kickoff
python seed_local.py @all
```

**Phase 2 — Hetzner / Telegram quick commands (from Windows PowerShell):**
```powershell
# SSH to the Hetzner box
ssh root@195.201.31.11

# Pull updated code + rebuild + restart (one liner — run on Hetzner)
# cd /root/wechat-x-youtube && git pull && docker build -t wechat-x-youtube . && \
#   docker stop wechat-x-youtube && docker rm wechat-x-youtube && \
#   docker run -d --name wechat-x-youtube --restart=always --network n8n_default \
#     --env-file /root/wechat-x-youtube/.env \
#     -v /root/wechat-x-youtube-data/video-output:/data/video-output \
#     -v /root/wechat-x-youtube-data/cookies:/data/cookies \
#     wechat-x-youtube

# Tail app logs (on Hetzner)
# docker logs -f wechat-x-youtube

# Re-register webhook (from anywhere with internet — replace TOKEN)
Invoke-RestMethod "https://tg.tianrenyuan.com/webhook/telegram/set_webhook?public_url=https://tg.tianrenyuan.com"

# Verify webhook
Invoke-RestMethod https://tg.tianrenyuan.com/webhook/telegram/info
```

**Phase 3 — Media downloader quick commands (on Hetzner):**
```bash
# Cobalt container management
docker ps | grep cobalt
docker logs cobalt-api --tail 20
docker restart cobalt-api

# List downloaded files (host-side path)
ls -lh /root/wechat-x-youtube-data/video-output/

# Test cobalt API directly
docker exec wechat-x-youtube curl -s -X POST http://cobalt-api:9000/ \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"}'

# Clean up old downloads
rm /root/wechat-x-youtube-data/video-output/*.mp4
```

```powershell
# Pull files from Hetzner to laptop (on Windows PowerShell)
scp "root@195.201.31.11:/root/wechat-x-youtube-data/video-output/*.mp4" `
    "D:\personal\ai_projects\56.wechat_collection\video-output\"
```

**Key URLs — Phase 1 (WeCom on SCF):**
- App: https://wecom.tianrenyuan.com/
- Health: https://wecom.tianrenyuan.com/health
- WeCom callback: https://wecom.tianrenyuan.com/webhook/wecom
- Test endpoint: https://wecom.tianrenyuan.com/webhook/wecom/test
- Seed endpoint (only works if SCF IP is whitelisted — rarely usable): https://wecom.tianrenyuan.com/webhook/wecom/seed?user=@all
- SCF console: https://console.cloud.tencent.com/scf
- **WeCom Admin Backend** (visibility, IP whitelist, address book): https://work.weixin.qq.com/
- WeCom Developer Center (callback Token/AESKey config only): https://developer.work.weixin.qq.com/

**Key URLs — Phase 2 (Telegram on Hetzner Germany):**
- App: https://tg.tianrenyuan.com/
- Health: https://tg.tianrenyuan.com/health
- Telegram webhook: https://tg.tianrenyuan.com/webhook/telegram
- Webhook info: https://tg.tianrenyuan.com/webhook/telegram/info
- Bot: https://t.me/wechatxyoutube_bot
- Hetzner Cloud console: https://console.hetzner.cloud/
- Repo: https://github.com/qizhangumich/wechat-x-youtube
- Find your Telegram numeric user ID: message `@userinfobot` on Telegram

**Key paths — Phase 3 (downloader):**
- Project on Hetzner: `/root/wechat-x-youtube/` (git clone, code only)
- Data on Hetzner: `/root/wechat-x-youtube-data/` (downloads, cookies; NOT in git)
  - Videos: `/root/wechat-x-youtube-data/video-output/`
  - Cookies: `/root/wechat-x-youtube-data/cookies/youtube_cookies.txt`
- Container view (in Notion's `Local File` column): `/data/video-output/...`
- Local download mirror: `D:\personal\ai_projects\56.wechat_collection\video-output\`

**Notion:**
- Notion integrations: https://www.notion.so/my-integrations

---

## 15. Media downloader (Phase 3)

Beyond collecting URLs, the system can automatically download the actual media (video/audio) for offline access. Built on `yt-dlp` for most sources, with self-hosted `cobalt.tools` routing for YouTube specifically.

### 15.1 — Two ways to trigger a download

| Trigger | Latency | Best for |
|---|---|---|
| **`/dl <url>` in Telegram** | ~1 second to start | "Save and grab now" from your phone |
| **Set `Download Status = Requested` on a Notion row** | up to 30 s (poll interval) | Going through your Notion inbox and picking which to keep |

Both routes call the same downloader → same Notion update → same file destination.

### 15.2 — Telegram commands

| Message | Result |
|---|---|
| `https://x.com/.../status/...` (plain URL) | Saves URL only, no download |
| `/dl https://x.com/.../status/...` | Saves URL + downloads video |
| `/dl audio https://www.youtube.com/...` | Saves URL + extracts audio-only MP3 |
| `/dla https://...` | Shorthand for `/dl audio ...` |

### 15.3 — Source routing rules

| Source | Tool | Notes |
|---|---|---|
| X / Twitter, TikTok, Bilibili, Vimeo, Reddit, 1500+ sites | `yt-dlp` direct | Works without proxy. 720p video by default |
| **YouTube** | `cobalt` (separate container) | yt-dlp gets IP-blocked from data-center IPs; cobalt's alternative extraction paths succeed |
| WeChat Channels | Not downloaded | Closed ecosystem — no public download path. URL is still saved |

### 15.4 — Cobalt container (one-time setup)

Cobalt runs as a sibling Docker container on the same `n8n_default` network:

```bash
docker run -d \
  --name cobalt-api \
  --restart=always \
  --network n8n_default \
  -e API_URL="http://cobalt-api:9000/" \
  -e DURATION_LIMIT=10800 \
  ghcr.io/imputnet/cobalt:10
```

Our app reaches it via internal Docker DNS at `http://cobalt-api:9000/` — never exposed publicly. Verify health:

```bash
docker logs cobalt-api --tail 10        # Should show "cobalt API ^ω^" banner
```

### 15.5 — Notion schema additions (6 new properties)

Create these on the database manually (case + space sensitive):

| Property | Type | Options |
|---|---|---|
| `Download Status` | Select | `Requested`, `Downloading`, `Done`, `Failed` |
| `Local File` | Text | (container path, see §15.6) |
| `File Size MB` | Number | — |
| `Duration` | Text | hh:mm:ss |
| `Downloaded At` | Date | — |
| `Download Error` | Text | (populated on failure) |

### 15.6 — File storage: container path ≠ host path

The `Local File` column in Notion shows the **container's** view of the path. To find files on the actual Hetzner disk:

```
Notion / container view:   /data/video-output/<title>.mp4
                                  ↕  (Docker -v bind mount)
Hetzner host / SFTP view:  /root/wechat-x-youtube-data/video-output/<title>.mp4
```

To retrieve to laptop:
```powershell
scp "root@195.201.31.11:/root/wechat-x-youtube-data/video-output/*.mp4" `
    "D:\personal\ai_projects\56.wechat_collection\video-output\"
```

Or browse with WinSCP / FileZilla → `root@195.201.31.11` → `/root/wechat-x-youtube-data/video-output/`.

### 15.7 — YouTube quality trade-off (and the proxy option)

| Setup | YouTube quality | Cost |
|---|---|---|
| cobalt only (current) | **240p only** (legacy format YouTube serves to data-center IPs) | $0 |
| cobalt + residential proxy | 720p+ | ~$3/mo |

YouTube refuses to serve modern HD formats from data-center IPs even when login isn't strictly required — they cap whatever does come through to a deliberately low-quality stream as a form of soft anti-scraping.

To unlock HD via a residential proxy:

1. Sign up at https://dataimpulse.com (or Smartproxy / IPRoyal) — pick Residential tier
2. Get your proxy URL: `http://USER:PASS@gw.dataimpulse.com:823`
3. Edit `/root/wechat-x-youtube/.env`, add: `PROXY_URL=http://USER:PASS@gw.dataimpulse.com:823`
4. Recreate cobalt with the proxy env var:
   ```bash
   PROXY_URL="http://USER:PASS@gw.dataimpulse.com:823"
   docker stop cobalt-api && docker rm cobalt-api
   docker run -d --name cobalt-api --restart=always --network n8n_default \
     -e API_URL="http://cobalt-api:9000/" \
     -e API_EXTERNAL_PROXY="$PROXY_URL" \
     -e DURATION_LIMIT=10800 \
     ghcr.io/imputnet/cobalt:10
   ```
5. Restart our app to pick up `PROXY_URL` for yt-dlp fallbacks:
   ```bash
   docker restart wechat-x-youtube
   ```

X / TikTok / Bilibili etc. don't have YouTube's IP-block problem — they continue working at full quality with or without the proxy.

### 15.8 — Cookies for yt-dlp (optional)

Cobalt handles YouTube without cookies. If you want yt-dlp to attempt YouTube as a backup (or for sites that require login):

1. Install browser extension **"Get cookies.txt LOCALLY"** (Chrome/Firefox)
2. Visit youtube.com while logged in, export → save as `youtube_cookies.txt`
3. Upload to Hetzner:
   ```powershell
   scp "D:\personal\ai_projects\56.wechat_collection\youtube_cookies.txt" `
       root@195.201.31.11:/root/wechat-x-youtube-data/cookies/
   ssh root@195.201.31.11 "chmod 600 /root/wechat-x-youtube-data/cookies/youtube_cookies.txt"
   ```
4. Container mount (already in the standard `docker run`):
   ```
   -v /root/wechat-x-youtube-data/cookies:/data/cookies
   ```

Code automatically **stages a copy to `/tmp/yt_dlp_cookies.txt`** inside the container before each call, so yt-dlp can update the working copy without corrupting your source. Source file on host stays pristine.

⚠️ Cookies = account password equivalent. Treat the file like a secret: `chmod 600`, `.gitignore` (already covered by `*_cookies.txt` rule), never commit.

### 15.9 — Common failures

| Symptom | Cause | Fix |
|---|---|---|
| YouTube `Sign in to confirm you're not a bot` | yt-dlp IP-blocked | Route through cobalt (default); for HD add proxy (§15.7) |
| Bot replies `queued` but Notion never updates | Existing row is on an old DB the integration was disconnected from | Re-invite integration to the current DB; or delete the stale row and retry |
| `[Errno 36] File name too long` | CJK title × non-byte-aware truncation | Already fixed via `.80B` byte-count truncation in template |
| `Read-only file system: /data/cookies/...` | Cookies mounted with `:ro` | Drop `:ro` from the `-v` mount; code stages to `/tmp/` for safety |
| `[downloader] yt-dlp not installed` | Stale image after Docker layer cache miss | Force rebuild: `docker build --build-arg YTDLP_REBUILD=$(date +%s) -t wechat-x-youtube .` |
| Cobalt unreachable from our app | `cobalt-api` container not on `n8n_default` network | `docker network inspect n8n_default` to confirm both containers listed |
