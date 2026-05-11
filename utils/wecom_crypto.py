"""
WeCom (Enterprise WeChat) message encryption/decryption helpers.

Implements the AES-CBC scheme described in the WeCom callback docs:
https://developer.work.weixin.qq.com/document/path/90968

Layout of the decrypted plaintext:
    [ random(16B) | msg_len(4B big-endian) | msg(msg_len B) | receive_id ]

`receive_id` for an internal app callback equals the CorpID (企业ID).
"""

import base64
import hashlib
import os
import socket
import struct
import time
from typing import Tuple

from Crypto.Cipher import AES


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def verify_signature(token: str, timestamp: str, nonce: str, encrypted: str,
                     msg_signature: str) -> bool:
    """SHA1 of sorted(token, timestamp, nonce, encrypted) hex-digest must match."""
    parts = sorted([token, timestamp, nonce, encrypted])
    expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    return expected == msg_signature


# ---------------------------------------------------------------------------
# Decryption (used on every inbound message and on URL-verify echostr)
# ---------------------------------------------------------------------------

def _aes_key_bytes(encoding_aes_key: str) -> bytes:
    """The console gives a 43-char base64 string; append '=' before decoding."""
    return base64.b64decode(encoding_aes_key + "=")


def decrypt(encoding_aes_key: str, encrypted_b64: str,
            expected_receive_id: str = "") -> str:
    """
    AES-256-CBC decrypt + strip PKCS#7 + parse WeCom envelope.

    Returns the inner plaintext message (or echostr).
    Raises ValueError if receive_id mismatches the expected CorpID.
    """
    key = _aes_key_bytes(encoding_aes_key)
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(base64.b64decode(encrypted_b64))

    # PKCS#7 unpad
    pad = raw[-1]
    if 1 <= pad <= 32:
        raw = raw[:-pad]

    # Skip 16 random bytes, read 4-byte big-endian length
    content = raw[16:]
    msg_len = struct.unpack(">I", content[:4])[0]
    msg = content[4:4 + msg_len].decode("utf-8")
    receive_id = content[4 + msg_len:].decode("utf-8")

    if expected_receive_id and receive_id.strip() != expected_receive_id.strip():
        raise ValueError(
            f"receive_id mismatch: expected '{expected_receive_id}', "
            f"got '{receive_id}' — check WECOM_CORP_ID in .env"
        )
    return msg


# ---------------------------------------------------------------------------
# Encryption (only needed if you want to PUSH messages back to WeCom)
# ---------------------------------------------------------------------------

def encrypt(encoding_aes_key: str, plaintext: str, receive_id: str) -> str:
    """Build the WeCom envelope, AES-CBC encrypt, base64 encode."""
    key = _aes_key_bytes(encoding_aes_key)
    iv = key[:16]

    rand16 = os.urandom(16)
    msg = plaintext.encode("utf-8")
    body = rand16 + struct.pack(">I", len(msg)) + msg + receive_id.encode("utf-8")

    # PKCS#7 pad to 32-byte block (WeCom spec uses block size 32)
    pad_len = 32 - (len(body) % 32)
    body += bytes([pad_len]) * pad_len

    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(body)).decode("ascii")


def make_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """Re-use of verify_signature's hash, exposed for response building."""
    parts = sorted([token, timestamp, nonce, encrypted])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def fresh_nonce_timestamp() -> Tuple[str, str]:
    return str(int(time.time())), socket.gethostname() + os.urandom(4).hex()
