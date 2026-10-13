# -*- coding: utf-8 -*-
"""
加密工具（與網頁端 WebCrypto 相容）
  金鑰：PBKDF2-HMAC-SHA256（250,000 次）→ 256-bit
  加密：AES-256-GCM
  格式：{"v":1,"kdf":"pbkdf2-sha256","iter":250000,"salt":b64,"iv":b64,"ct":b64}
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 250_000


def _key(password: str, salt: bytes, iterations: int = ITER) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(data: bytes, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, data, None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "pbkdf2-sha256", "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(obj: dict, password: str) -> bytes:
    d = lambda k: base64.b64decode(obj[k])
    return AESGCM(_key(password, d("salt"), obj.get("iter", ITER))).decrypt(d("iv"), d("ct"), None)


def encrypt_json(payload, password: str) -> str:
    return json.dumps(encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                              password))
