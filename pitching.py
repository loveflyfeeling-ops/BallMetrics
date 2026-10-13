# -*- coding: utf-8 -*-
"""
先發投手近況模組（MLB、中職）

資料：data/history/<聯盟>_pitching.csv
  MLB：MLB 官方 statsapi 投手逐場紀錄（gameLog）
  CPBL：中職官網「逐場成績表」（見 cpbl_feed.py）

近況指標（賽前，只用比賽日之前的紀錄，不偷看未來）
  1. 每場先發的 FIP 成分 = (13×全壘打 + 3×(四壞+觸身) − 2×三振) / 局數
     FIP 只看投手自己能控制的結果，比 ERA 更能預測未來。
  2. 指數加權：越近的先發權重越高（半衰期 hl 場，由訓練自動挑選）。
  3. 樣本收縮：加上 prior 局「聯盟平均」的虛擬紀錄，樣本少的投手（新秀、傷癒）不會被極端化。
  4. 預期局數：同樣加權並向 5 局收縮；吃局數越多的投手影響越大。
  5. 每場可省下的失分 = (聯盟平均 − 投手指標) × 預期局數 / 9
     換算 Elo 加減分 = c × 省下失分（c 由訓練自動挑選）。
"""
from __future__ import annotations

import csv
import json
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
HIST = BASE / "data" / "history"
PITCH_FILE = HIST / "MLB_pitching.csv"  # 相容舊程式


def pitch_file(league: str = "MLB") -> Path:
    return HIST / f"{league.upper()}_pitching.csv"
PITCH_FIELDS = ["pid", "name", "season", "date", "game_pk", "is_home", "gs", "outs", "er", "hr", "bb", "hbp", "k", "bf"]
UA = {"User-Agent": "Mozilla/5.0 BallMetrics/3.0"}

DEFAULT_IP = 5.0      # 預期局數的收縮目標
IP_PRIOR_STARTS = 2   # 預期局數的虛擬場數


def norm_name(s: str) -> str:
    """比對用名字：去重音、小寫、去標點；中文名保留。"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().replace(".", " ").replace("-", " ").replace("・", " ").replace("．", " ")
    s = s.split("(")[0].split("（")[0]  # 去掉 (RHP) 之類的註記
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# 抓取 / 存取
# ---------------------------------------------------------------------------
def _get(url: str, timeout: int = 30, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_logs(pairs: set[tuple[int, int]], names: dict[int, str] | None = None, workers: int = 12) -> list[dict]:
    """pairs = {(投手ID, 球季)}，回傳逐場紀錄（含先發與後援）。"""
    names = names or {}

    def one(pair):
        pid, season = pair
        try:
            d = _get(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=pitching&season={season}&gameType=R,F,D,L,W")  # 含季後賽
        except Exception:
            return []
        out = []
        for st in d.get("stats", []):
            for sp in st.get("splits", []):
                s = sp.get("stat", {})
                out.append({
                    "pid": pid, "name": names.get(pid) or sp.get("player", {}).get("fullName", ""),
                    "season": str(season), "date": sp.get("date", ""), "game_pk": sp.get("game", {}).get("gamePk"),
                    "is_home": 1 if sp.get("isHome") else 0, "gs": int(s.get("gamesStarted", 0) or 0),
                    "outs": int(s.get("outs", 0) or 0), "er": int(s.get("earnedRuns", 0) or 0),
                    "hr": int(s.get("homeRuns", 0) or 0), "bb": int(s.get("baseOnBalls", 0) or 0),
                    "hbp": int(s.get("hitByPitch", 0) or 0), "k": int(s.get("strikeOuts", 0) or 0),
                    "bf": int(s.get("battersFaced", 0) or 0),
                })
        return out

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, sorted(pairs)):
            rows.extend(r)
    return rows


_caches: dict[str, dict] = {}


def load_logs(league: str = "MLB") -> list[dict]:
    pf = pitch_file(league)
    _cache = _caches.setdefault(league.upper(), {"mtime": None, "logs": None})
    if not pf.exists():
        return []
    mt = pf.stat().st_mtime
    if _cache["mtime"] == mt:
        return _cache["logs"]
    rows = []
    with pf.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"pid": int(r["pid"]), "name": r["name"], "season": r["season"], "date": r["date"],
                             "game_pk": int(r["game_pk"]) if r["game_pk"] else None, "is_home": int(r["is_home"]),
                             **{k: int(r[k]) for k in ("gs", "outs", "er", "hr", "bb", "hbp", "k", "bf")}})
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["date"])
    idx: dict[int, list] = {}
    for r in rows:
        if r["gs"] == 1:
            idx.setdefault(r["pid"], []).append(r)
    _cache.update(mtime=mt, logs=rows, idx=idx)
    return rows


def save_logs(rows: list[dict], replace: bool = False, league: str = "MLB") -> int:
    old = [] if replace else list(load_logs(league))
    seen = {(r["pid"], r["game_pk"]) for r in old}
    added = 0
    for r in rows:
        k = (r["pid"], r["game_pk"])
        if k not in seen:
            seen.add(k)
            old.append(r)
            added += 1
    old.sort(key=lambda r: r["date"])
    pf = pitch_file(league)
    pf.parent.mkdir(parents=True, exist_ok=True)
    with pf.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PITCH_FIELDS)
        w.writeheader()
        for r in old:
            w.writerow({k: r.get(k, "") for k in PITCH_FIELDS})
    return added


def actual_starters(logs: list[dict]) -> dict[int, dict]:
    """由逐場紀錄找出每場實際先發：game_pk → {'home': pid, 'away': pid}"""
    out: dict[int, dict] = {}
    for r in logs:
        if r["gs"] == 1 and r["game_pk"]:
            out.setdefault(r["game_pk"], {})["home" if r["is_home"] else "away"] = r["pid"]
    return out


# ---------------------------------------------------------------------------
# 近況指標
# ---------------------------------------------------------------------------
def fip_num(r: dict) -> float:
    return 13 * r["hr"] + 3 * (r["bb"] + r["hbp"]) - 2 * r["k"]


def league_consts(logs: list[dict], seasons: set[str] | None = None) -> dict:
    """聯盟先發投手平均（只用指定球季，避免偷看驗證季）。"""
    num = ip = er = 0.0
    for r in logs:
        if r["gs"] != 1 or (seasons is not None and r["season"] not in seasons):
            continue
        num += fip_num(r)
        ip += r["outs"] / 3
        er += r["er"]
    lg_raw = num / ip if ip else 0.0
    lg_era = 9 * er / ip if ip else 4.2
    return {"lg_raw": lg_raw, "cfip": lg_era - lg_raw, "lg_era": lg_era}


class FormTracker:
    """依時間順序累積每位投手的加權近況。"""

    def __init__(self, hl: float, prior_ip: float, offset: float, lg_raw: float):
        self.d = 0.5 ** (1.0 / hl)
        self.prior_ip = prior_ip
        self.offset = offset
        self.lg_raw = lg_raw
        self.s: dict[int, list[float]] = {}  # pid -> [w_num, w_ip, w_starts]

    def add(self, r: dict):
        if r["gs"] != 1:
            return
        st = self.s.setdefault(r["pid"], [0.0, 0.0, 0.0])
        ip = r["outs"] / 3
        st[0] = st[0] * self.d + fip_num(r)
        st[1] = st[1] * self.d + ip
        st[2] = st[2] * self.d + 1.0

    def value(self, pid) -> tuple[float, float, float]:
        """回傳 (指標 raw, 預期局數, 每場省下失分)。未知投手以『聯盟平均 + 懲罰』估計。"""
        mean = self.lg_raw + self.offset
        st = self.s.get(pid) if pid else None
        if not st:
            raw, ipx = mean, DEFAULT_IP - 0.5
        else:
            raw = (st[0] + self.prior_ip * mean) / (st[1] + self.prior_ip)
            ipx = (st[1] + DEFAULT_IP * IP_PRIOR_STARTS) / (st[2] + IP_PRIOR_STARTS)
        return raw, ipx, (self.lg_raw - raw) * ipx / 9.0


def game_values(rows: list[dict], logs: list[dict], hl: float, prior_ip: float, offset: float, lg_raw: float,
                covered: set | None = None):
    """對每場比賽（依日期排序）算出賽前雙方先發的省下失分，回傳 ([(home_rs, away_rs)], tracker)。
    covered：投手資料完整的球季；不在其中的球季不做投手調整（缺資料不等於爛投手）。"""
    tr = FormTracker(hl, prior_ip, offset, lg_raw)
    starts = [r for r in logs if r["gs"] == 1]
    i, n = 0, len(starts)
    out = []
    for g in rows:
        while i < n and starts[i]["date"] < g["date"]:
            tr.add(starts[i])
            i += 1
        if covered is not None and g["season"] not in covered:
            out.append((0.0, 0.0))
            continue
        hp, ap = g.get("home_sp_id"), g.get("away_sp_id")
        out.append((tr.value(hp)[2], tr.value(ap)[2]))
    while i < n:  # 剩餘紀錄（最新）也納入，供未來預測
        tr.add(starts[i])
        i += 1
    return out, tr


def recent_starts(pid: int, n: int = 5, league: str = "MLB") -> dict | None:
    """最近 n 場先發摘要（顯示用）。"""
    load_logs(league)
    logs = (_caches.get(league.upper(), {}).get("idx") or {}).get(pid, [])
    if not logs:
        return None
    last = logs[-n:]
    ip = sum(r["outs"] for r in last) / 3
    er = sum(r["er"] for r in last)
    return {
        "starts": len(last),
        "ip": round(ip, 1),
        "era": round(9 * er / ip, 2) if ip else None,
        "k": sum(r["k"] for r in last),
        "bb": sum(r["bb"] for r in last),
        "hr": sum(r["hr"] for r in last),
        "last_date": last[-1]["date"],
        "games": [{"date": r["date"][5:], "ip": f"{r['outs'] // 3}.{r['outs'] % 3}", "er": r["er"], "k": r["k"]}
                  for r in reversed(last)],
    }
