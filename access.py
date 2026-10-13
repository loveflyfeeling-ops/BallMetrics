# -*- coding: utf-8 -*-
"""
手機連線與安全

連線方式
  1. 同一個 Wi-Fi：手機開 http://<電腦區網IP>:8000
  2. 外出（4G/5G）：Cloudflare 免費快速通道（cloudflared），產生 https://xxxx.trycloudflare.com 網址
     ※ 網址每次重開會改變；需先設定 PIN 才能開啟

保護
  - 電腦本機（127.0.0.1）免登入
  - 其他裝置：有設定 PIN 就必須登入；透過遠端通道進來的一律必須登入
  - PIN 以 PBKDF2 雜湊儲存；登入後發 30 天有效的簽章 Cookie；登入失敗過多會暫時鎖定
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ACCESS_FILE = BASE / "data" / "access.json"
COOKIE = "bm_session"
SESSION_DAYS = 30


# ---------------------------------------------------------------------------
# 設定存取
# ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ACCESS_FILE)


def _secret() -> bytes:
    d = _load()
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(32)
        _save(d)
    return d["secret"].encode()


def pin_set() -> bool:
    return bool(_load().get("pin_hash"))


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def set_pin(new_pin: str) -> None:
    if not re.fullmatch(r"\d{4,8}", new_pin or ""):
        raise ValueError("PIN 需為 4~8 位數字")
    d = _load()
    salt = secrets.token_hex(16)
    d.update(pin_salt=salt, pin_hash=_hash(new_pin, salt))
    d["secret"] = secrets.token_hex(32)  # 換 PIN 時讓所有舊登入失效
    _save(d)


def clear_pin() -> None:
    d = _load()
    d.pop("pin_hash", None)
    d.pop("pin_salt", None)
    d["secret"] = secrets.token_hex(32)
    _save(d)


def check_pin(pin: str) -> bool:
    d = _load()
    if not d.get("pin_hash"):
        return False
    return hmac.compare_digest(_hash(pin or "", d["pin_salt"]), d["pin_hash"])


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
def make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(tok: str | None) -> bool:
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


# 登入失敗限制（每個 IP：10 分鐘內 5 次）
_fails: dict[str, list[float]] = {}


def login_blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= 5


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------------------
# 來源判斷
# ---------------------------------------------------------------------------
PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "cf-ray")


def classify(client_host: str | None, headers) -> str:
    """回傳 local（電腦本機）/ lan（同網段）/ remote（網路通道或外部）。"""
    if any(h in headers for h in PROXY_HEADERS):
        return "remote"  # 透過 cloudflared 進來的請求在本機看起來是 127.0.0.1，但會帶這些標頭
    host = (client_host or "").strip()
    if host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return "local"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_link_local:
            return "lan"
    except ValueError:
        pass
    return "remote"


def needs_login(kind: str) -> bool:
    if kind == "local":
        return False
    if kind == "remote":
        return True  # 遠端一律要 PIN（沒設 PIN 就無法登入＝拒絕）
    return pin_set()  # 區網：有設 PIN 才需要


def lan_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不會真的送封包，只用來找出對外網卡 IP
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ipaddress.ip_address(ip).is_private and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# ---------------------------------------------------------------------------
# Cloudflare 快速通道
# ---------------------------------------------------------------------------
class Tunnel:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self._lock = threading.Lock()

    @staticmethod
    def exe() -> str | None:
        for name in ("cloudflared.exe", "cloudflared"):
            p = BASE / name
            if p.exists():
                return str(p)
        return shutil.which("cloudflared")

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def status(self) -> dict:
        return {"installed": bool(self.exe()), "running": self.running(), "url": self.url if self.running() else None,
                "error": self.error}

    def start(self, port: int = 8000) -> dict:
        with self._lock:
            if self.running():
                return self.status()
            exe = self.exe()
            if not exe:
                self.error = "找不到 cloudflared，請先執行「啟用遠端連線.bat」下載"
                return self.status()
            if not pin_set():
                self.error = "請先設定 PIN 才能開啟遠端連線"
                return self.status()
            self.url, self.error = None, None
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore",
                creationflags=flags)
            self.started_at = time.time()
            threading.Thread(target=self._read, daemon=True).start()
        for _ in range(40):  # 最多等 20 秒取得網址
            if self.url or not self.running():
                break
            time.sleep(0.5)
        if not self.url and not self.running():
            self.error = self.error or "cloudflared 啟動失敗"
        return self.status()

    def _read(self):
        pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        for line in self.proc.stdout:
            m = pat.search(line)
            if m and not self.url:
                self.url = m.group(0)
            if "ERR" in line and not self.url:
                self.error = line.strip()[-200:]

    def stop(self) -> dict:
        with self._lock:
            if self.running():
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except Exception:
                    self.proc.kill()
            self.proc, self.url = None, None
        return self.status()


tunnel = Tunnel()
