#!/usr/bin/env python3
"""
seed_local.py — Run from your laptop (NOT SCF) to send a kickoff message
that creates the bot conversation in recipients' WeCom chat lists.

Why local instead of SCF?
  WeCom requires whitelisting the IP that calls its API. SCF rotates its
  outbound IP across hundreds of addresses, making whitelisting impractical.
  Your laptop has one stable public IP — whitelist it once, run this once,
  done.

Usage:
  1. Find your laptop's public IP:
       https://ifconfig.me   or   https://whatismyip.com
  2. Add it to WeCom whitelist:
       https://work.weixin.qq.com → 应用管理 → wechat-x-youtube
       → 企业可信IP → 配置 → add your IP → save
  3. Run this script from this project directory:
       python seed_local.py                # default: @all
       python seed_local.py Jeremy         # one user
       python seed_local.py "Jeremy,zhangqi"  # multiple users
       python seed_local.py @all "Custom welcome message"

After it runs successfully, open WeCom (mobile or desktop) → 消息 tab —
the bot conversation will be at the top.
"""

import os
import sys

import requests
from dotenv import load_dotenv

# Load env vars from .env in this directory
load_dotenv()

CORP_ID     = os.getenv("WECOM_CORP_ID", "").strip()
CORP_SECRET = os.getenv("WECOM_CORP_SECRET", "").strip()
AGENT_ID    = os.getenv("WECOM_AGENT_ID", "").strip()

DEFAULT_MESSAGE = (
    "👋 Hello! I'm your link saver bot.\n\n"
    "Forward any link to me here and I'll save it to your Notion database. "
    "WeChat Channels videos, public account articles, anything with a URL works."
)


def main() -> None:
    if not (CORP_ID and CORP_SECRET and AGENT_ID):
        print("ERROR: Missing WECOM_CORP_ID / WECOM_CORP_SECRET / WECOM_AGENT_ID in .env")
        sys.exit(1)

    user_arg = sys.argv[1] if len(sys.argv) > 1 else "@all"
    message  = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MESSAGE

    print(f"→ Recipients: {user_arg}")
    print(f"→ Message preview: {message[:60]}{'...' if len(message) > 60 else ''}")
    print()

    # --- 1. Fetch access_token ------------------------------------------
    print("Fetching access_token...")
    tok = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": CORP_ID, "corpsecret": CORP_SECRET},
        timeout=10,
    ).json()
    if tok.get("errcode") != 0:
        print(f"gettoken failed: {tok}")
        if tok.get("errcode") == 60020:
            print("\n⚠ IP not whitelisted. Steps:")
            print("  1. Visit https://ifconfig.me to find your current public IP")
            print("  2. Add it at https://work.weixin.qq.com → 应用管理 → wechat-x-youtube → 企业可信IP")
        sys.exit(1)
    access_token = tok["access_token"]
    print(f"OK access_token: {access_token[:20]}...")
    print()

    # --- 2. Build payload -------------------------------------------------
    pipe_users = "|".join(u.strip() for u in user_arg.split(",") if u.strip())
    payload = {
        "touser": pipe_users,
        "msgtype": "text",
        "agentid": int(AGENT_ID),
        "text": {"content": message},
        "safe": 0,
    }

    # --- 3. Send message --------------------------------------------------
    print(f"Sending message...")
    resp = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
        json=payload,
        timeout=10,
    ).json()

    print(f"WeCom response: {resp}")
    print()

    if resp.get("errcode") == 0:
        delivered = pipe_users
        invalid = resp.get("invaliduser", "")
        if invalid:
            delivered = "|".join(u for u in pipe_users.split("|") if u not in invalid.split("|"))
            print(f"⚠ Skipped (not joined / not in Allowed Users): {invalid}")
        print(f"OK Delivered to: {delivered or '(none — all skipped)'}")
        print()
        print("Open WeCom (mobile or desktop) → 消息 tab — bot conversation is at the top.")
    else:
        print(f"FAILED: {resp.get('errmsg')}")
        if resp.get("errcode") == 60020:
            print("\n⚠ IP whitelist issue — see top of script for fix.")
        sys.exit(1)


if __name__ == "__main__":
    main()
