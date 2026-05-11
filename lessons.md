# Lessons Learned — WeCom → Notion Link Collector

A retrospective of every wrong turn we took building this system, what each one looked like, what actually caused it, and the principle to take into the next project.

---

## Category A — Infrastructure & domain setup

### A1. ngrok free domain ≠ valid for WeCom

| | |
|---|---|
| **What we tried** | Use `https://rudolf-cingulated-overplausibly.ngrok-free.dev/webhook/wecom` as the WeCom callback URL during development |
| **Symptom** | WeCom console: **"Domain entity verification failed"** (域名主体校验失败) |
| **Root cause** | WeCom requires the callback domain to have a Chinese **ICP filing (备案)** under an entity that matches your WeCom registered entity. Free ngrok domains are not ICP-filed |
| **Fix** | Filed a personal/company-owned `.com` (`tianrenyuan.com`) with ICP, used `wecom.tianrenyuan.com` as a subdomain |
| **Lesson** | For any callback URL into a WeCom/WeChat/Tencent product, plan the ICP filing **before** writing code. Treat it like a 2–3 week dependency, not a side quest |

---

### A2. ICP entity must match WeCom corporate entity

| | |
|---|---|
| **What we tried** | ICP-filed under company entity ✅, but assumed any company entity would work |
| **Symptom** | After ICP approval, WeCom still showed **"Domain entity verification failed"** |
| **Root cause** | WeCom matches the **legal entity name on the ICP filing** against the **legal entity registered in WeCom** (企业全称). Mismatch = blocked, even if both are "your" companies |
| **Fix** | Confirmed both said exactly `上海天人圆科技有限公司`; then waited for WeCom's ICP cache to refresh (≤72h after filing approval) |
| **Lesson** | Two checks aren't enough — they must be **the same string**. Decide which entity owns the domain *and* the WeCom corp before filing |

---

### A3. WeCom's ICP cache has a multi-day lag

| | |
|---|---|
| **What we tried** | Save callback URL immediately after ICP approval came through |
| **Symptom** | "Domain entity verification failed" persisted for hours despite filing being approved |
| **Root cause** | WeCom doesn't query the ICP database in real time — it has its own cache that refreshes on the order of 24–72 hours |
| **Fix** | Wait. There's no API to force-refresh |
| **Lesson** | When a Chinese platform says "verification failed" right after a registry change, the first hypothesis should be **cache lag**, not config error. Burn one debug cycle confirming, then walk away for a day |

---

### A4. DNS not propagated until SCF custom domain is bound to the *correct* alias

| | |
|---|---|
| **What we tried** | Created CNAME `wecom.tianrenyuan.com → 1255478305.ap-guangzhou.tencentscf.com` immediately after binding the custom domain |
| **Symptom** | `nslookup wecom.tianrenyuan.com` returned no IP. Browser: `DNS_PROBE_FINISHED_NXDOMAIN` |
| **Root cause** | SCF only "activates" the A record for the CNAME target after you bind the custom domain to the **right alias** (`默认流量` aka `$DEFAULT`, not `$LATEST`) |
| **Fix** | Re-bound custom domain to `default/wecom-collector/$DEFAULT`; DNS started resolving within seconds |
| **Lesson** | "DNS not resolving" can be **upstream**, not propagation. Before waiting for DNS, verify the target service is actually serving on that hostname |

---

### A5. Function URL alias matters: `$LATEST` vs `$DEFAULT`

| | |
|---|---|
| **What we tried** | Bound custom domain to whichever alias appeared first in the dropdown (`$LATEST`) |
| **Symptom** | Custom domain returned `ERR_INVALID_RESPONSE` / 404, even though Function URL worked when called directly |
| **Root cause** | `$LATEST` = the most recent unpublished code; `默认流量 ($DEFAULT)` = what's behind the published alias custom domains expect |
| **Fix** | Re-bound custom domain to `$DEFAULT` |
| **Lesson** | In Tencent SCF, **always** bind custom domains and triggers to `$DEFAULT` unless you have a specific blue/green reason not to |

---

### A6. WeCom has TWO consoles and they do different things

| | |
|---|---|
| **What we tried** | Looked for "Allowed users" / 可见范围 in https://developer.work.weixin.qq.com |
| **Symptom** | Couldn't find visibility settings; user reported "no 可见范围" |
| **Root cause** | WeCom splits config across two consoles: **Developer Center** (developer.work.weixin.qq.com) only shows API/callback settings; **Admin Backend** (work.weixin.qq.com) holds visibility, IP whitelist, address book, member management |
| **Fix** | Always start at https://work.weixin.qq.com for member-related changes; use the developer center only for callback Token/AESKey |
| **Lesson** | Chinese platforms often split admin and developer surfaces. When a "missing" setting can't be found in one, the answer is almost always "wrong console" |

---

### A7. SCF outbound IPs rotate — you cannot whitelist them

| | |
|---|---|
| **What we tried** | Whitelist `119.29.194.90` (IP from one SCF invocation) in WeCom 企业可信IP, then call the seed endpoint again |
| **Symptom** | Error 60020 again, but now from `1.12.73.141` — completely different /16 |
| **Root cause** | SCF picks an outbound IP from a large pool per cold start. Different invocations can come from totally different ranges |
| **Fix** | Stop trying to whitelist SCF. Instead, run any IP-whitelisted outbound call from a **machine with a stable IPv4** — your laptop. Created `seed_local.py` for this |
| **Lesson** | If a remote API requires IP whitelisting and your serverless platform doesn't offer fixed egress (without paid VPC + NAT), **don't host that API call in serverless**. Use a stable-IP runner: laptop, fixed VM, or NAT-bound function. Inbound (callback) traffic doesn't need this — only outbound |

---

### A8. Hetzner self-host replaced Fly.io (infra consolidation)

| | |
|---|---|
| **What we tried** | Fly.io Singapore as the home for the Telegram path (outside the GFW, free tier covers it) |
| **Symptom** | Worked, but introduced a third place for secrets (laptop `.env`, SCF env vars, Fly secrets), a third deployment workflow (`fly deploy`), and ~$1-2/mo recurring after free credit expired |
| **Root cause** | We already had a Hetzner Germany VPS running n8n + Caddy. Adding a second cloud platform was infrastructure sprawl for no functional gain |
| **Fix** | Migrated Telegram side to Hetzner. Container shares the existing n8n Caddy reverse proxy and the `n8n_default` Docker network. One less place to remember |
| **Lesson** | If you already have a Linux VPS with HTTPS + Docker for one project, you don't need a second platform "just for webhooks." Consolidate. The cognitive overhead of multi-cloud is real even at personal scale |

---

## Category B — Build & deployment

### B1. PowerShell `Compress-Archive` produces a zip SCF rejects

| | |
|---|---|
| **What we tried** | `Compress-Archive -Path . -DestinationPath deploy.zip` |
| **Symptom** | SCF upload error: **"Unzip codezip Failed: zip code format error"** |
| **Root cause** | PowerShell uses a non-standard zip variant (likely an older spec) that SCF's unzipper can't parse |
| **Fix** | Built the zip with Python's `zipfile` module instead: `python -c "import zipfile, os; ..."` |
| **Lesson** | Cloud functions are pickier about zip format than you think. When in doubt, use the language's stdlib zipper, not the OS shell zipper |

---

### B2. Building a zip from `C:\Windows\System32`

| | |
|---|---|
| **What we tried** | Ran the zip command without changing directory first |
| **Symptom** | `Access denied` errors during compression |
| **Root cause** | The PowerShell session opened in `C:\Windows\System32` (Windows default for elevated shells); zip was trying to write there |
| **Fix** | `cd D:\personal\ai_projects\56.wechat_collection` before running the zip command |
| **Lesson** | First command in any shell session: print the working directory. Saves five mysteries per project |

---

### B3. SCF can't reach PyPI from inside the function

| | |
|---|---|
| **What we tried** | Let SCF do `pip install -r requirements.txt` at runtime |
| **Symptom** | `ERROR: No matching distribution found for fastapi==0.111.0` then `from versions: <empty>` |
| **Root cause** | SCF's runtime environment has restricted/blocked outbound to public PyPI. The pip mirror it ships with may be outdated or unreachable |
| **Fix** | Locally install Linux-platform wheels into `./packages/`, bundle into the deploy zip, set `PYTHONPATH=/var/user/packages` in `scf_bootstrap` |
| **Lesson** | For any Chinese cloud serverless platform, bundle dependencies. Don't trust the runtime to fetch them |

---

### B4. `pip install` defaults to host platform (Windows wheels useless on Linux SCF)

| | |
|---|---|
| **What we tried** | `pip install -t ./packages -r requirements.txt` on Windows |
| **Symptom** | Worked locally; on SCF, `ImportError` for any package with native code (e.g., `pycryptodome`) |
| **Root cause** | Pip installed `.whl` files compiled for Windows. SCF runs Linux |
| **Fix** | Explicit cross-platform install: `pip install --platform manylinux2014_x86_64 --python-version 3.10 --only-binary=:all: --implementation cp -t ./packages -r requirements.txt` |
| **Lesson** | When bundling deps for cross-platform deploy, **always** pin platform/version/implementation flags. Windows wheels and Linux wheels are different binaries |

---

### B5. SCF's `/var/lang/python3` is a lying symlink

| | |
|---|---|
| **What we tried** | `scf_bootstrap` ran `/var/lang/python3/bin/python3 -m uvicorn ...` |
| **Symptom** | `libssl.so.10: cannot open shared object file: No such file or directory` |
| **Root cause** | Despite the SCF console showing "Python 3.10", `/var/lang/python3/bin/python3` is actually Python **3.6** (linked against an old `libssl.so.10`). The Python 3.10 binary lives at `/var/lang/python310/bin/python3.10` |
| **Fix** | Used the explicit path `/var/lang/python310/bin/python3.10` in `scf_bootstrap` |
| **Lesson** | Don't trust `python3`/`python` symlinks in serverless containers. Discover the actual interpreters with `ls /var/lang/` and pin the full path |

---

### B6. Pinning ancient FastAPI to silence dep warnings

| | |
|---|---|
| **What we tried** | Originally `fastapi==0.111.0` — wouldn't install on SCF. Tried newer versions one by one |
| **Symptom** | `pydantic 2.x` requires Python 3.7+, our SCF Python 3.10 was fine, but the bundled wheels mismatch with Pydantic v2 surface area changes |
| **Root cause** | `fastapi==0.68.0` pairs with `pydantic 1.8.x` and `starlette 0.14.x` — older but rock-solid combo that bundles cleanly on `manylinux2014` |
| **Fix** | Pinned the matched-set: `fastapi==0.68.0 + uvicorn==0.15.0 + pydantic==1.8.2 + python-dotenv==0.19.0` |
| **Lesson** | For one-off serverless deployments, pick a **known-good triplet** of (FastAPI, Pydantic, Starlette). Don't try to use the latest of each — the ecosystem cliff between Pydantic 1 and 2 is too sharp |

---

### B8. CJK characters use 3 bytes — Linux filename limit is 255 bytes, not chars

| | |
|---|---|
| **What we tried** | yt-dlp output template `%(title).100s` — truncate to 100 characters |
| **Symptom** | `[Errno 36] File name too long` errors during merge step on Chinese titles, even after .100s truncation |
| **Root cause** | Linux's filesystem limit is **255 bytes**, not 255 chars. UTF-8 Chinese chars are **3 bytes each**, so 100 chars = ~300 bytes. Add yt-dlp's intermediate suffix like `.fhls-510.mp4.part-Frag494.part` (~35 bytes) and the path overflows |
| **Fix** | Use `%(title).80B` — yt-dlp supports a byte-count modifier (`B`). 80 bytes ≈ 26 CJK chars or 80 ASCII chars, both safely under the 255-byte limit |
| **Lesson** | When truncating user-supplied strings for filesystem use, **always count bytes, not characters**. The 4× safety factor your gut applies to char-based limits isn't a safety factor at all for CJK / emoji / any 3-4-byte UTF-8 content |

---

### B7. Local dependency conflicts panicked us during install

| | |
|---|---|
| **What we tried** | `pip install -t ./packages -r requirements.txt` |
| **Symptom** | Long list of red errors: `gradio 6.11.0 requires fastapi>=0.115.2 ...`, `openai 2.30.0 requires pydantic<3,>=1.9.0 ...` |
| **Root cause** | Pip's resolver also checks **globally installed** packages (gradio, openai, anthropic in your main env). The conflicts were against your other projects, not against `./packages` |
| **Fix** | Ignored the warnings — the actual install into `./packages` succeeded. Verified with `Test-Path .\packages\Crypto\Cipher\AES.py` |
| **Lesson** | When installing with `-t <dir>`, only the `Successfully installed ...` line matters. Conflict warnings about your global env are noise — but you should still scan them for the package you care about |

---

## Category C — WeCom integration

### C1. Plain-Text mode is no longer offered by WeCom

| | |
|---|---|
| **What we tried** | Started with the assumption that WeCom would offer "明文模式" (Plain Text Mode) for testing |
| **Symptom** | New WeCom dev console form has Token AND EncodingAESKey both required, no mode selector |
| **Root cause** | WeCom deprecated Plain-Text mode for self-built apps. **Encrypted mode is mandatory** |
| **Fix** | Implemented AES-256-CBC decrypt in `utils/wecom_crypto.py` and wired it into the GET (echostr) and POST (Encrypt envelope) handlers |
| **Lesson** | Old documentation and old SDK examples lie. Open the actual console form **before** designing the auth flow |

---

### C2. Trailing space in `WECOM_CORP_ID` env var

| | |
|---|---|
| **What we tried** | Pasted CorpID `wwc58f0762e5e753ec ` from console (trailing space invisible) |
| **Symptom** | If we'd hit it: `receive_id mismatch: expected 'wwc58f0762e5e753ec ', got 'wwc58f0762e5e753ec'` |
| **Root cause** | Console form trimmed the value when displaying but our `.env` paste kept the space |
| **Fix** | Removed trailing space; added defensive `.strip()` in `wecom_crypto.py` |
| **Lesson** | Always trim env-var values you compare for equality. Defense in depth — don't rely on the user pasting clean strings |

---

### C3. Smoke test passed = code deployed (one-line proof, but we forgot it)

| | |
|---|---|
| **What we tried** | Updated AES code locally, asked WeCom to verify the URL |
| **Symptom** | WeCom save still failed; SCF Platform log showed `200, 1ms duration` |
| **Root cause** | We hadn't actually re-uploaded `deploy.zip` after the code change. The 1ms duration was the smoking gun — real AES decrypt takes 30–80ms |
| **Fix** | Added a one-line smoke-test convention: `curl "https://.../webhook/wecom?echostr=test123"` should return 400 (encrypted mode rejects). If it returns 200, old code is still live |
| **Lesson** | After every deploy, run **one diagnostic curl that cannot pass under the old behavior**. Don't trust UI ✓s as evidence the deploy happened |

---

### C4. `SCF_Type:Platform` logs are useless for app debugging

| | |
|---|---|
| **What we tried** | Looked at the SCF log entry that came back from a failed callback |
| **Symptom** | All it showed was `Report RequestId: ... Duration: 1ms StatusCode: 200` — no app info |
| **Root cause** | The SCF log filter defaults to `SCF_Type:Platform` (request lifecycle metadata). Our `logger.info`/`logger.warning` calls are `SCF_Type:Custom` |
| **Fix** | **Clear all filters**, search by RequestId — both Platform and Custom logs appear together |
| **Lesson** | Always show your colleague (or future self) how to find the *actual* app logs as part of the runbook. The default view is a trap |

---

### C5. WeCom self-built apps don't passively read group messages

| | |
|---|---|
| **What we hoped** | Add the bot to a group, anyone can paste a link, all get captured |
| **Symptom** | (Would have been: nothing happens when members post links) |
| **Root cause** | WeCom design: self-built apps only receive messages where they are **explicitly @-mentioned**. Passive group capture requires the enterprise-tier 会话内容存档 (Conversation Content Archive) |
| **Fix** | Documented the @-mention pattern. For passive capture, would need a separate archive worker — out of scope for personal use. Pivoted to **1-on-1 chat with the bot** as the primary flow |
| **Lesson** | Read the platform's message routing rules **before** designing UX. WeCom isn't a chat — it's a directed message bus |

---

### C6. WeCom self-built apps don't show as direct chat contacts until seeded

| | |
|---|---|
| **What we tried** | After configuring callback + visibility correctly, looked for the bot in WeCom mobile **工作台** and **通讯录 → 应用** sections |
| **Symptom** | Bot icon either absent, or tapping it offered to set an "App Homepage" web URL — no chat box anywhere |
| **Root cause** | WeCom self-built apps are designed as one-way notification channels by default. The 1-on-1 chat conversation thread only exists *after* the bot has sent the user at least one message |
| **Fix** | Added a `seed_local.py` script that uses the corp_secret to call WeCom's Send Message API and push a "hello" from the bot to the user. After that, the conversation lives in the user's 消息 tab forever and they can reply |
| **Lesson** | Many enterprise messaging platforms have "API-only" relationships by default — the user needs an existing conversation thread before they can talk back. Plan to ship a bootstrap step (or trigger one auto-magically on first interaction via another channel) |

---

### C7. 未加入 (Not Joined) members are address-book ghosts

| | |
|---|---|
| **What we encountered** | WeCom address book showed two `Jeremy` entries — one with 未加入 status |
| **Symptom** | Confusing; couldn't tell which was the "real" account; uncertain whether seed message would deliver |
| **Root cause** | An admin can add anyone to the address book by name + phone/email. The person doesn't appear as a usable account until they accept the invitation, install WeCom, and log in. Until then, 未加入 = invitation pending |
| **Fix** | Use `@all` for seed broadcast — WeCom auto-skips unjoined users and returns them in `invaliduser`. Optionally clean up stale unjoined profiles via the admin backend |
| **Lesson** | "Member exists" ≠ "member can receive messages." Always check the activation/joined status before assuming an ID is reachable |

---

### C8. WeCom display name ≠ UserID

| | |
|---|---|
| **What we tried** | Used `Jeremy` (the display name visible in the desktop client) as the UserID in seed API calls |
| **Symptom** | Could've failed with `errcode: 40003 invalid userid` — though we got lucky and it matched |
| **Root cause** | The desktop client only shows a human-readable display name. The internal UserID may be `Jeremy`, `JeremyChen`, `JeremyAt`, or completely auto-generated. Only visible in https://work.weixin.qq.com → 通讯录 → click member → 账号 field |
| **Fix** | Documented where to find UserID. Encouraged use of `@all` to skip the discovery step entirely |
| **Lesson** | Display strings are for humans; internal IDs are for APIs. Never assume they match — always look up the canonical ID before automating against a user |

---

### C9. WeCom 企业可信IP whitelist is IPv4-only

| | |
|---|---|
| **What we tried** | Got laptop's public IP via `Invoke-RestMethod https://ifconfig.me` |
| **Symptom** | Returned an IPv6 address (`2406:3003:...`). WeCom's whitelist form rejects 8-hex-group inputs |
| **Root cause** | Many ISPs (especially mobile in China) prefer IPv6 by default. ifconfig.me + browsers will return whichever the OS prefers. WeCom legacy whitelist only accepts IPv4 dotted-quad |
| **Fix** | Used `Invoke-RestMethod https://api.ipify.org` instead — this service forces IPv4 |
| **Lesson** | When a third party demands "your IP," explicitly ask for the v4 form. Tools that auto-detect can hand you v6 and there's no warning that it'll be rejected downstream |

---

### C10. WeChat Channels videos arrive as `MsgType=link`, not `text`

| | |
|---|---|
| **What we tried** | Initial parser only handled `MsgType=text`, assuming users would paste plain URLs |
| **Symptom** | Sharing a Channels video from personal WeChat to the WeCom bot would silently log "Ignoring non-text message (MsgType=link)" — nothing in Notion |
| **Root cause** | When you "Share to WeCom" any rich content (article, Channels video, etc.), WeCom packages it as a **link card** with separate `<Url>`, `<Title>`, `<Description>` fields. The MsgType becomes `link`, not `text` |
| **Fix** | Extended `_extract_text_from_xml` to handle `MsgType=link` by combining the three fields into a synthetic text body. Also added a diagnostic log line that prints the full decrypted XML so future unknown types are visible |
| **Lesson** | Don't assume the chat platform delivers everything as plain text. Forwarded/shared content typically arrives in structured formats. **Log the raw payload** for at least the first few real-world messages so you see what fields exist |

---

### C12. YouTube hard-blocks data center IPs — cookies don't help

| | |
|---|---|
| **What we tried** | yt-dlp on Hetzner with valid logged-in YouTube cookies, all four player clients (tv, android, ios, web), latest yt-dlp version |
| **Symptom** | Every attempt: `Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication.` Same error with or without cookies, with or without auth tokens |
| **Root cause** | YouTube actively flags data-center IP ranges (Hetzner, AWS, GCP, etc.) regardless of auth. Their bot check correlates IP geo, IP ASN, and cookies; a "real-user cookie set from a data-center IP" reads as suspicious. Cookies *expand* the rejection footprint — yt-dlp's auto-update strips your auth tokens on failure |
| **Fix (partial, free)** | Self-host **cobalt.tools** as a sibling container. Cobalt uses different extraction paths (mobile-app endpoints) and YouTube *does* serve some content to it — but typically only legacy 240p formats |
| **Fix (full quality, $3/mo)** | Residential proxy (DataImpulse, Smartproxy, IPRoyal). yt-dlp `--proxy` flag plus cobalt's `API_EXTERNAL_PROXY` env var both route through a real consumer IP |
| **Lesson** | Site-by-site IP reputation is **asymmetric**: the same Hetzner IP that fails for YouTube works fine for X, TikTok, Bilibili. Don't assume one workaround applies everywhere. For YouTube specifically, "automated download from cloud" is a hard commercial problem they've actively chosen to make difficult |

---

### C11. The "Set Workspace Tool Page" dialog was a red herring

| | |
|---|---|
| **What we encountered** | When clicking the bot's settings, a dialog asked us to set an "App Homepage" URL — webpage or mini-program |
| **Symptom** | Got distracted trying to figure out what URL to put there |
| **Root cause** | This homepage URL is for *opening a custom dashboard* when users tap the app icon in 工作台. It's optional and **irrelevant** for our use case (we want a chat, not a webpage) |
| **Fix** | Cancel the dialog. Don't set anything |
| **Lesson** | Platforms often surface optional features as if they were required. When a config form appears unprompted, ask "what happens if I cancel this?" before filling it in |

---

## Category D — Notion integration

### D1. Notion API returns 404 for "no permission" — not 403

| | |
|---|---|
| **What we tried** | Created Notion DB, set `NOTION_DATABASE_ID` env var, hit the test endpoint |
| **Symptom** | `404 Client Error: Not Found for url: https://api.notion.com/v1/pages` |
| **Root cause** | Created a brand-new database and forgot to invite the integration to it via **⋯ → Connections → + Add connections** |
| **Fix** | Added the integration to the database |
| **Lesson** | Notion deliberately returns 404 (not 403) for unauthorized resources to avoid leaking existence. **Inviting the integration is a manual per-database step every time** — there's no global access |

---

### D2. Updated `.env` locally, forgot SCF env vars

| | |
|---|---|
| **What we tried** | Changed `NOTION_DATABASE_ID` in `.env` after switching to a new database |
| **Symptom** | Test endpoint returned 404 from Notion (continued targeting old DB) |
| **Root cause** | SCF reads from console env vars, not from `.env` in the deploy bundle. `.env` is dev-only |
| **Fix** | Updated `NOTION_DATABASE_ID` in SCF console → 函数配置 → 环境变量. Env-var changes apply immediately, no redeploy needed |
| **Lesson** | Maintain a checklist: "Where does this value need to be?" — local `.env`, SCF console, WeCom console, Notion DB title… A single value can live in 3+ places |

---

### D4. Container path ≠ host path in user-facing Notion fields

| | |
|---|---|
| **What we shipped** | The downloader wrote `link.file_path` (from yt-dlp's stdout) directly to Notion's `Local File` property |
| **Symptom** | Path in Notion is `/data/video-output/<title>.mp4`, but `ls /data/video-output/` on Hetzner fails — no such directory exists on the host |
| **Root cause** | yt-dlp runs inside the container where the Docker bind mount maps `/root/wechat-x-youtube-data/video-output/` (host) → `/data/video-output/` (container). The path our app emits is the container's view. The user looks for it via SSH/SFTP and sees nothing |
| **Fix (documentation)** | Documented the mapping in `guide.md` §15.6 with a 2-row table so future-you knows where to actually look |
| **Fix (code, not yet applied)** | Substitute the host path when writing to Notion. Or expose a configurable `FILE_PATH_PREFIX_SWAP` env var |
| **Lesson** | When an app's filesystem view differs from the host's (Docker, virtualization, chroot), any path the app records as a **user-facing identifier** needs translation. Containers are an abstraction leak for anything humans interact with. Logging container paths internally is fine; surfacing them in Notion / Slack / emails isn't |

---

### D3. Notion column names are case-sensitive AND space-sensitive

| | |
|---|---|
| **Risk we mitigated** | If our DB had `Original Url` instead of `Original URL`, Notion would 400 with `validation_error: 'Original URL' is not a property that exists` |
| **Fix in code** | Centralized all column names as constants at top of `notion_client.py` (`PROP_TITLE`, `PROP_ORIGINAL_URL`, …). One place to rename |
| **Lesson** | Externalize identifiers (column names, env var keys, route paths) as named constants — even when they're "obviously" stable. Future-you renames the DB and grep finds every reference |

---

## Category E — Process & debugging style

### E1. "It returned 200 so it worked" — false friend

Multiple times across this project, a 200 response masked broken behavior:
- WeCom callback returning encrypted echostr unchanged → 200 OK from server, but WeCom rejection
- `/webhook/wecom?echostr=test123` returning `test123` → 200 OK, but means old code

**Lesson:** A 200 status code only means "TCP connection didn't break." For each endpoint, define a **second-level check** (response body shape, latency profile, log line presence) and treat 200 + anomaly as a failure.

---

### E2. Long debug sessions where the real bug was a redeploy that didn't happen

We spent at least 2 round-trips chasing "why does the AES code return the wrong thing" when the actual answer was "the AES code wasn't running yet."

**Lesson:** Before debugging behavior, prove the code under test is the code that's executing. Print a version string, a build hash, or *something* on startup. `logger.info("Starting WeCom Collector v1.2 (build abc123)")`.

---

### E3. Multiple values for the same logical config

`WECOM_CORP_ID` lived in:
1. WeCom Admin → 我的企业 → 企业信息
2. Local `.env`
3. SCF env vars

Three sources, three chances to drift. Only one is "the truth" (#1) but our code reads #3.

**Lesson:** For each config value, identify the **canonical source** and document who's responsible for syncing copies. A `MAINTAINER_NOTES.md` or section in the runbook listing "if you change X in WeCom, you must also update SCF env var Y" prevents 80% of "config drift" outages.

---

### E4. We wrote AES-decrypt code with a TODO and shipped it

The original `routes/wecom.py` had:
```python
# If WECOM_ENCODING_AES_KEY is set, decrypt echostr here (TODO).
# For now, return as-is (works for Plain-Text mode).
return echostr
```

This TODO would have been fine if Plain-Text mode were still an option. It wasn't, so we wasted a deploy cycle discovering this in production.

**Lesson:** TODOs in code paths that depend on platform-side configuration are landmines. Either implement them upfront, or wrap them in a `raise NotImplementedError("Plain-Text mode no longer supported by WeCom — implement AES")` so the failure is loud, not silent.

---

### E5. The bootstrap problem — chicken-and-egg with platform onboarding

We hit two flavors of "you need X to do Y, and X requires Y to set up":

1. **The seed conversation problem**: To chat with the bot, the user needs the bot to message them first. To message them, we need an API call from a whitelisted IP. To whitelist an IP, we need to know which IP we'll call from. SCF rotates IPs, so we couldn't whitelist proactively.

2. **The "find the bot in the UI" problem**: The bot appears in user's 消息 tab only after it has spoken. So the *first* contact must come via API, not via UI exploration.

**Lesson:** Every two-way integration has a "first message" problem. Identify it early and design a bootstrap path. Often the bootstrap lives **outside** the eventual production code path (a one-time local script, an admin button, a manual API call from Postman). That's fine — it's still part of the system, just a different runtime.

---

### E6. Read-only mounts for "secret" files conflict with tools that rotate them

| | |
|---|---|
| **What we tried** | Mounted YouTube cookies file with Docker `:ro` flag to prevent yt-dlp from accidentally corrupting it |
| **Symptom** | `OSError: [Errno 30] Read-only file system: '/data/cookies/youtube_cookies.txt'` — yt-dlp crashed mid-download |
| **Root cause** | yt-dlp **updates** the cookies file after each authenticated call (this is normal — Set-Cookie headers from YouTube get persisted back). The `:ro` flag we added defensively prevented this benign rewrite |
| **Fix (round 1, wrong)** | Dropped `:ro`. yt-dlp then **shredded** the cookies file: when YouTube rejected the auth on a bot-check page, yt-dlp wrote back the leaner cookie set from that response, losing all auth tokens |
| **Fix (round 2, right)** | Code **copies cookies to `/tmp/yt_dlp_cookies.txt` inside the container** before each yt-dlp call. yt-dlp updates its working copy freely; the host source file stays pristine. Mount can stay `rw` or `ro` (doesn't matter — we don't write to source) |
| **Lesson** | "Lock it down" instincts can collide with how a tool actually expects to operate. When you decide a file must be read-only, **check first whether the consumer expects to write to it**. Often the cleaner answer is a **copy-on-use** pattern (preserves source, lets consumer scratch as needed), not access restriction |

---

### E7. Stale page_ids from dedup hits outlive the parent database

| | |
|---|---|
| **What we tried** | `/dl <url>` dedup'd against an earlier test row (the URL already saved). Code looked up the existing `page_id` and updated it with download results |
| **Symptom** | Download finished successfully (file on disk), but Notion update failed with `400 Bad Request`. Error logging didn't include Notion's response body so we couldn't see why |
| **Root cause** | The dedup-matched `page_id` was on an **older Notion database** we'd migrated away from. The new "Download Status" property didn't exist on that DB → property validation 400 |
| **Fix** | (1) Improved `update_page()` to log Notion's response body on failure (so next 400 is diagnosable in one log line). (2) Documented: when you migrate Notion DBs, the new DB starts dedup-empty, but old saved rows still come back via `find_page_id_by_url` matching |
| **Lesson** | When you `raise_for_status()` on a 4xx response, **always log the response body too**. Notion's bodies explain exactly which property failed validation. Generic `400 Bad Request` is one of the least useful error strings in computing |

---

## Category F — What went *right* (worth replicating)

A retro isn't only a list of mistakes. These choices paid off:

| Decision | Why it helped |
|---|---|
| Centralized config in `Settings` class with `validate()` at startup | Bad/missing env vars surfaced on first request, not 30 minutes into traffic |
| `/webhook/wecom/test` endpoint that bypasses signature/encryption | Could verify the entire Notion pipeline in seconds without WeCom involvement |
| Dedup via MD5 of normalized URL stored as a Notion property | Survives WeCom's automatic retries gracefully — no duplicate rows on transient failures |
| Catching all exceptions in the WeCom POST handler and always returning `"success"` | WeCom retries forever on 5xx; we'd have multiplied any error 10x without this |
| Pydantic models for every cross-boundary structure (`ProcessResult`, `LinkSaveResult`) | Makes the JSON contract self-documenting and validates at the boundary |
| Bundled all Linux deps into `packages/` rather than relying on SCF's pip | One artifact, fully reproducible, immune to upstream PyPI/mirror flakiness |
| Smoke-test convention (`?echostr=test123` curl) | Single command tells us deploy status + env var presence |

---

## Top 10 takeaways for the next project

1. **For Chinese platform integrations, ICP filing is a 2–3 week dependency.** Plan it as critical path, not a nice-to-have.
2. **Bundle dependencies; don't trust runtime pip.** Especially in serverless, especially in regulated regions.
3. **After every deploy, run a diagnostic that fails under the *previous* behavior.** A 200 response is not proof of anything except network.
4. **Externalize and document every config value.** Note its canonical source and every place it's copied to.
5. **Implement TODOs that depend on external systems immediately, or guard them with loud failures.** Silent fallbacks become production bugs the moment the assumed mode disappears.
6. **Identify the bootstrap problem early.** Every two-way integration has a "first message" chicken-and-egg. Plan a one-time path (local script, manual API call) that lives outside the production runtime.
7. **Don't host IP-whitelisted outbound calls in serverless.** Serverless egress IPs rotate. Inbound callbacks are fine on serverless; whitelisted *outbound* needs a stable-IP runner — your laptop, a fixed VM, or a function bound to a NAT gateway.
8. **Count bytes, not characters.** Filesystem limits, database column widths, and many protocol fields are byte-bounded. UTF-8 CJK chars are 3 bytes each — a "100-char" string can be 300 bytes. Truncate by bytes when the destination cares about bytes.
9. **Use copy-on-use for "read-only-ish" files.** When a tool expects to update a file but you want to preserve your golden source, the cleanest pattern is to copy the source to a scratch location at call time. Cleaner than fighting with `:ro` mounts or restoring after the fact.
10. **Don't surface container paths in user-facing fields.** Container filesystem != host filesystem. Anything humans will look at (Notion props, log dashboards, emails) needs the host-side path. Container paths are an abstraction leak for the human-facing layer.

---

## Quick reference: where each lesson surfaced in code

| Lesson | Code/config artifact |
|---|---|
| A6 (two consoles) | guide.md §1, §11 troubleshooting |
| A7 (SCF IP rotation) | `seed_local.py` exists *because* of this |
| A8 (Hetzner over Fly) | Dockerfile, no fly.toml in production path |
| B8 (byte truncation) | `services/downloader.py` — `%(title).80B` |
| C1 (Plain-Text deprecated) | `utils/wecom_crypto.py` |
| C6 (must seed conversation) | `seed_local.py` + guide.md §8 |
| C10 (Channels = link card) | `routes/wecom.py::_extract_text_from_xml` MsgType=link branch |
| C12 (YouTube IP-block) | `services/cobalt_client.py` exists *because* of this |
| D1 (Notion 404 = no permission) | guide.md §11 troubleshooting |
| D4 (container vs host paths) | guide.md §15.6 |
| E5 (bootstrap problem) | `seed_local.py` is the bootstrap-runtime artifact |
| E6 (cookies copy-on-use) | `services/downloader.py::_stage_cookies` |
| E7 (log response bodies on 4xx) | `services/notion_client.py::update_page` |

