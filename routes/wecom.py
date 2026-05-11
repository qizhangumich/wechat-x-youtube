"""
WeCom (Enterprise WeChat) callback endpoints.

Two routes are provided:

1. GET /webhook/wecom
   URL ownership verification — WeCom calls this once when you first configure
   the callback URL in the developer console. It sends an encrypted `echostr`
   that must be decrypted and returned as plain text.

   STATUS: skeleton implementation. Signature verification and AES decryption
   are marked with TODO comments — implement once you have the WeCom console
   credentials. For MVP purposes you can set "Plain Text Mode" in the WeCom
   console to skip encryption, in which case echostr can be echoed directly.

2. POST /webhook/wecom
   Receives actual messages from WeCom, parses the XML body, and hands the
   text content off to the processing pipeline.

3. POST /webhook/wecom/test    ← local development only
   Accepts a simple JSON body {"text": "..."} so you can test the full
   pipeline without configuring WeCom at all.
"""

import hashlib
import hmac
import xml.etree.ElementTree as ET
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from config import settings
from models.schemas import ProcessResult, TestMessageRequest
from services.message_service import process_message
from utils.logger import get_logger
from utils.wecom_crypto import decrypt as wecom_decrypt, verify_signature

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook/wecom", tags=["wecom"])


# ---------------------------------------------------------------------------
# 1. GET — URL ownership verification
# ---------------------------------------------------------------------------

@router.get("", response_class=PlainTextResponse)
async def wecom_verify(
    msg_signature: Optional[str] = Query(None),
    timestamp: Optional[str] = Query(None),
    nonce: Optional[str] = Query(None),
    echostr: Optional[str] = Query(None),
) -> str:
    """
    WeCom calls this endpoint to verify that you own the callback URL.

    In Plain-Text mode (no encryption): WeCom sends a raw `echostr` and
    expects it back unchanged.  Enable this mode in the WeCom developer
    console while doing initial testing.

    In Encrypted mode: `echostr` is AES-encrypted.  You must:
      1. Verify the signature (msg_signature)
      2. Decrypt echostr with your EncodingAESKey
      3. Return the decrypted string as plain text

    TODO (Encrypted mode):
      - Implement verify_signature(token, timestamp, nonce, encrypted_echostr)
      - Implement aes_decrypt(encoding_aes_key, encrypted_echostr) → plain_echostr
      Both helpers belong in a new module  utils/wecom_crypto.py  to keep this
      file clean.
    """
    if not echostr:
        raise HTTPException(status_code=400, detail="Missing echostr parameter")

    # --- Plain-Text mode (no AES key configured) ----------------------------
    if not settings.WECOM_ENCODING_AES_KEY:
        logger.warning(
            "WECOM_ENCODING_AES_KEY not set — returning echostr unchanged "
            "(Plain-Text mode). Will NOT pass URL verification in encrypted mode."
        )
        return echostr

    # --- Encrypted mode (Token + EncodingAESKey both configured) ------------
    if not (msg_signature and timestamp and nonce):
        raise HTTPException(
            status_code=400,
            detail="Missing msg_signature/timestamp/nonce — encrypted mode requires all three",
        )

    if not verify_signature(settings.WECOM_TOKEN, timestamp, nonce, echostr, msg_signature):
        logger.warning(
            "WeCom signature mismatch on verification request "
            f"(timestamp={timestamp}, nonce={nonce})"
        )
        raise HTTPException(status_code=403, detail="Signature mismatch")

    try:
        plain = wecom_decrypt(
            settings.WECOM_ENCODING_AES_KEY,
            echostr,
            expected_receive_id=settings.WECOM_CORP_ID,
        )
    except Exception as exc:
        logger.error(f"echostr decryption failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Decryption error: {exc}")

    logger.info(f"WeCom URL verification OK — returning {len(plain)}-char plaintext")
    return plain


# ---------------------------------------------------------------------------
# 2. POST — receive WeCom messages
# ---------------------------------------------------------------------------

@router.post("", response_class=PlainTextResponse)
async def wecom_callback(
    request: Request,
    msg_signature: Optional[str] = Query(None),
    timestamp: Optional[str] = Query(None),
    nonce: Optional[str] = Query(None),
) -> str:
    """
    Receive and process an incoming WeCom message.

    WeCom message format (XML, possibly encrypted):

        <xml>
          <ToUserName><![CDATA[ww...]]></ToUserName>
          <FromUserName><![CDATA[user_id]]></FromUserName>
          <CreateTime>1234567890</CreateTime>
          <MsgType><![CDATA[text]]></MsgType>
          <Content><![CDATA[the message text here]]></Content>
          <MsgId>123456</MsgId>
          <AgentID>1</AgentID>
        </xml>

    WeCom expects the response body to be the string "success".

    TODO (Encrypted mode):
      - If the XML contains an <Encrypt> tag, decrypt it using
        utils/wecom_crypto.py before parsing.
    """
    raw_body = await request.body()
    logger.info(f"WeCom callback received — body length: {len(raw_body)}")

    # --- If in encrypted mode, unwrap the <Encrypt> tag first --------------
    body_for_parsing = raw_body
    if settings.WECOM_ENCODING_AES_KEY:
        try:
            outer = ET.fromstring(raw_body)
            encrypt_el = outer.find("Encrypt")
            if encrypt_el is not None and encrypt_el.text:
                if not (msg_signature and timestamp and nonce):
                    logger.warning("Encrypted POST missing signature params")
                    return "success"
                if not verify_signature(
                    settings.WECOM_TOKEN, timestamp, nonce,
                    encrypt_el.text, msg_signature,
                ):
                    logger.warning("WeCom POST signature mismatch")
                    return "success"
                plain_xml = wecom_decrypt(
                    settings.WECOM_ENCODING_AES_KEY,
                    encrypt_el.text,
                    expected_receive_id=settings.WECOM_CORP_ID,
                )
                body_for_parsing = plain_xml.encode("utf-8")
        except ET.ParseError as exc:
            logger.warning(f"Outer XML parse failed: {exc}")
            return "success"
        except Exception as exc:
            logger.error(f"Decrypt failed: {exc}", exc_info=True)
            return "success"

    # --- Parse XML body -------------------------------------------------------
    content = _extract_text_from_xml(body_for_parsing)
    if content is None:
        # Non-text message types (image, voice, etc.) — acknowledge and ignore
        logger.info("Non-text message or unparseable body — acknowledging without processing")
        return "success"

    if not content.strip():
        logger.info("Empty text content — acknowledging without processing")
        return "success"

    # --- Run the processing pipeline -----------------------------------------
    try:
        result: ProcessResult = process_message(content, captured_from="WeCom")
        logger.info(f"Pipeline result: {result.message}")
    except Exception as exc:
        # Never return an error to WeCom — it will retry endlessly.
        # Log the failure and respond with "success" anyway.
        logger.error(f"Pipeline error: {exc}", exc_info=True)

    # WeCom requires exactly "success" as the response body
    return "success"


# ---------------------------------------------------------------------------
# 3. POST /test — local development helper
# ---------------------------------------------------------------------------

@router.get("/seed")
async def wecom_seed(
    user: Optional[str]    = Query(None, description="Comma-separated WeCom UserIDs, e.g. 'Jeremy,zhangqi'. Use '@all' to send to all members in the app's Allowed Users."),
    party: Optional[str]   = Query(None, description="Comma-separated department IDs (optional)"),
    tag: Optional[str]     = Query(None, description="Comma-separated tag IDs (optional)"),
    message: Optional[str] = Query(None, description="Override the default hello message (optional)"),
) -> dict:
    """
    Push a kickoff message from the app to one or more users, so the
    conversation appears in their WeCom chat list. After this runs once
    per user, each user can reply to the bot and every reply fires the
    callback that saves links to Notion.

    Examples:
      GET /webhook/wecom/seed?user=Jeremy
      GET /webhook/wecom/seed?user=Jeremy,zhangqi
      GET /webhook/wecom/seed?user=@all
      GET /webhook/wecom/seed?user=Jeremy&message=Hi%20again

    Requires WECOM_CORP_ID, WECOM_CORP_SECRET, WECOM_AGENT_ID in env.
    """
    import requests as req

    if not (settings.WECOM_CORP_ID and settings.WECOM_CORP_SECRET and settings.WECOM_AGENT_ID):
        raise HTTPException(
            status_code=500,
            detail="Missing one of WECOM_CORP_ID / WECOM_CORP_SECRET / WECOM_AGENT_ID env vars",
        )

    if not (user or party or tag):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of user / party / tag (use 'user=@all' to broadcast to all allowed members)",
        )

    # WeCom expects pipe-separated lists, so convert "a,b,c" -> "a|b|c"
    def _pipes(value: Optional[str]) -> str:
        if not value:
            return ""
        return "|".join(part.strip() for part in value.split(",") if part.strip())

    touser  = _pipes(user)
    toparty = _pipes(party)
    totag   = _pipes(tag)

    # 1. Fetch access_token
    tok_resp = req.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={
            "corpid": settings.WECOM_CORP_ID.strip(),
            "corpsecret": settings.WECOM_CORP_SECRET.strip(),
        },
        timeout=10,
    )
    tok_data = tok_resp.json()
    if tok_data.get("errcode") != 0:
        logger.error(f"gettoken failed: {tok_data}")
        raise HTTPException(status_code=500, detail=f"gettoken failed: {tok_data}")
    access_token = tok_data["access_token"]

    # 2. Send the kickoff message
    body_text = message or (
        "👋 Hello! I'm your link saver bot.\n\n"
        "Forward any link to me here and I'll save it to your Notion "
        "database. WeChat Channels videos, public account articles, "
        "anything with a URL works."
    )

    payload = {
        "msgtype": "text",
        "agentid": int(settings.WECOM_AGENT_ID),
        "text": {"content": body_text},
        "safe": 0,
    }
    if touser:
        payload["touser"] = touser
    if toparty:
        payload["toparty"] = toparty
    if totag:
        payload["totag"] = totag

    msg_resp = req.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
        json=payload,
        timeout=10,
    )
    msg_data = msg_resp.json()
    logger.info(
        f"Seed message — touser={touser} toparty={toparty} totag={totag} "
        f"errcode={msg_data.get('errcode')} errmsg={msg_data.get('errmsg')}"
    )

    if msg_data.get("errcode") != 0:
        raise HTTPException(
            status_code=500,
            detail=f"send message failed: {msg_data}",
        )

    return {
        "ok": True,
        "touser": touser,
        "toparty": toparty,
        "totag": totag,
        "message": "Kickoff sent. Each recipient should now see the bot conversation in WeCom 消息 tab.",
        "wecom_response": msg_data,
    }


@router.post("/test", response_model=ProcessResult)
async def wecom_test(payload: TestMessageRequest) -> ProcessResult:
    """
    Local test endpoint — bypasses all WeCom signature verification.

    POST /webhook/wecom/test
    Body: {"text": "Your text with https://links.here"}

    Returns the full ProcessResult so you can inspect what was saved.
    This endpoint is NOT gated by environment — keep in mind to restrict
    access in production (e.g. with an API key middleware or by disabling it).
    """
    logger.info(f"[test] Processing text: {payload.text[:120]}{'...' if len(payload.text) > 120 else ''}")
    result = process_message(payload.text, captured_from="Test Endpoint")
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_signature(token: str, timestamp: str, nonce: str, *extra: str) -> str:
    """
    WeCom signature = SHA1 of sorted concatenation of [token, timestamp, nonce, ...extras].
    Used for both URL verification and per-message signature checks.
    """
    parts = sorted([token, timestamp, nonce, *extra])
    raw = "".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _extract_text_from_xml(raw_body: bytes) -> Optional[str]:
    """
    Parse a WeCom XML message body and return text containing any URLs.

    Handles:
    - MsgType=text  → uses <Content>
    - MsgType=link  → uses <Url> + <Title> + <Description> (link cards from
                      shared posts, including WeChat Channels videos shared
                      from personal WeChat into a WeCom group)

    Returns None if:
    - The body is not valid XML
    - MsgType is something we don't yet handle (image/voice/video/event/...)

    DIAGNOSTIC: logs the full inbound XML at INFO level so we can see
    exotic message types (miniprogram, etc.) that we may need to handle.
    """
    # --- One-time diagnostic: log raw payload (truncated) ----------------
    try:
        preview = raw_body.decode("utf-8", errors="replace")
    except Exception:
        preview = repr(raw_body[:500])
    logger.info(f"Inbound XML (decrypted): {preview[:1200]}")

    try:
        root = ET.fromstring(raw_body)
    except ET.ParseError as exc:
        logger.warning(f"XML parse error: {exc}")
        return None

    msg_type_el = root.find("MsgType")
    msg_type = (msg_type_el.text or "").strip().lower() if msg_type_el is not None else "unknown"

    # --- Plain text message ------------------------------------------------
    if msg_type == "text":
        content_el = root.find("Content")
        if content_el is None:
            logger.warning("MsgType=text but no <Content> element")
            return None
        return content_el.text or ""

    # --- Link card (forwarded post / shared article / Channels video) ----
    if msg_type == "link":
        url_el         = root.find("Url")
        title_el       = root.find("Title")
        description_el = root.find("Description")

        url   = (url_el.text or "").strip()         if url_el         is not None else ""
        title = (title_el.text or "").strip()       if title_el       is not None else ""
        desc  = (description_el.text or "").strip() if description_el is not None else ""

        if not url:
            logger.warning("MsgType=link but no <Url> element")
            return None

        # Build a synthetic text body so the existing pipeline can find the URL
        # and so the Notion 'Raw Message' column has the human context too.
        parts = [url]
        if title:
            parts.append(f"[Title] {title}")
        if desc:
            parts.append(f"[Desc] {desc}")
        synthetic = " ".join(parts)
        logger.info(f"Link card normalized: url={url}  title={title!r}")
        return synthetic

    # --- Anything else ----------------------------------------------------
    logger.info(f"Ignoring unsupported message type (MsgType={msg_type})")
    return None
