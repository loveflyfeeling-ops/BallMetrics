# -*- coding: utf-8 -*-
"""
中華職棒資料同步（中職官網 www.cpbl.com.tw）

※ 中職官網只允許台灣網路連線，本模組需在台灣的電腦上執行。
※ 官網對密集請求會暫時封鎖，因此每次請求之間會刻意間隔，請勿調快。

資料來源
  /schedule/getgamedatas   整季賽程：比分、球場、雙方先發投手、勝敗投（一次一季一賽別）
  /team/getfollowscore     投手逐場成績：局數、自責分、三振、四壞、觸身、全壘打、先發/中繼
兩者都是官網頁面用的 AJAX 介面，需先開啟頁面取得 RequestVerificationToken 與 cookie。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
BASE_URL = "https://www.cpbl.com.tw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

KINDS = {"A": "例行賽", "E": "季後挑戰賽", "C": "台灣大賽"}
KIND_NUM = {"A": 1, "E": 2, "C": 3}
# 隊名統一（更名的球隊視為同一隊，Elo 才能延續）
TEAM_ALIAS = {"Lamigo": "樂天桃猿", "Lamigo桃猿": "樂天桃猿", "統一獅": "統一7-ELEVEn獅",
              "統一7-ELEVEn獅": "統一7-ELEVEn獅", "台鋼": "台鋼雄鷹"}


def team(name: str) -> str:
    n = (name or "").strip()
    return TEAM_ALIAS.get(n, n)


def game_pk(year, kind, sno) -> int:
    return int(f"{int(year)}{KIND_NUM.get(kind, 9)}{int(sno):04d}")


def pid_of(acnt) -> int | None:
    try:
        return int(acnt) if acnt else None
    except ValueError:
        return None


def ip_to_outs(ip) -> int:
    ip = float(ip or 0)
    return int(ip) * 3 + int(round((ip % 1) * 10))


# ---------------------------------------------------------------------------
# 官網連線
# ---------------------------------------------------------------------------
class CPBLClient:
    def __init__(self, delay: float = 0.8):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-TW,zh;q=0.9")]
        self.delay = delay
        self.tokens: dict[str, str] = {}
        self._last = 0.0

    def _wait(self):
        dt = time.time() - self._last
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self._last = time.time()

    def _get(self, path: str) -> str:
        self._wait()
        with self.op.open(BASE_URL + path, timeout=30) as r:
            return r.read().decode("utf-8", "ignore")

    def token(self, page: str, action: str) -> str:
        key = f"{page}|{action}"
        if key not in self.tokens:
            html = self._get(page)
            m = re.search(re.escape(action) + r"[\s\S]{0,600}?RequestVerificationToken:\s*'([^']+)'", html)
            if not m:
                raise RuntimeError(f"官網頁面 {page} 找不到 {action} 的驗證碼（官網改版或被暫時封鎖）")
            self.tokens[key] = m.group(1)
        return self.tokens[key]

    def post(self, page: str, action: str, data: dict, tries: int = 3) -> dict:
        last = None
        for i in range(tries):
            try:
                tok = self.token(page, action)
                self._wait()
                req = urllib.request.Request(
                    BASE_URL + action, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST",
                    headers={"RequestVerificationToken": tok, "X-Requested-With": "XMLHttpRequest",
                             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                             "Referer": BASE_URL + page, "Origin": BASE_URL})
                with self.op.open(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:  # 被擋時等久一點再試，並重新取得驗證碼
                last = e
                self.tokens.clear()
                time.sleep(5 * (i + 1))
        raise RuntimeError(f"中職官網連線失敗：{last}")

    def season_games(self, year: int, kind: str = "A") -> list[list]:
        r = self.post("/schedule", "/schedule/getgamedatas",
                      {"calendar": f"{year}/01/01", "location": "", "kindCode": kind, "teamNo": ""})
        return [raw_game(g) for g in json.loads(r.get("GameDatas") or "[]")]

    def pitcher_logs(self, acnt: str, year: int, kind: str = "A") -> list[list]:
        r = self.post(f"/team/follow?acnt={acnt}", "/team/getfollowscore",
                      {"acnt": acnt, "defendStation": "投手", "year": str(year), "kindCode": kind})
        return [raw_log(f) for f in json.loads(r.get("FollowScore") or "[]")]


# 與瀏覽器收集程式相同的精簡格式
def raw_game(g: dict) -> list:
    return [g.get("Year"), g.get("KindCode"), g.get("GameSno"), (g.get("PreExeDate") or g.get("GameDate") or "")[:16],
            g.get("VisitingTeamName"), g.get("HomeTeamName"), g.get("VisitingScore"), g.get("HomeScore"),
            g.get("VisitingPitcherAcnt") or "", g.get("VisitingPitcherName") or "",
            g.get("HomePitcherAcnt") or "", g.get("HomePitcherName") or "", g.get("FieldAbbe") or "",
            g.get("PresentStatus"), g.get("GameResult"), g.get("IsGameStop")]


def raw_log(f: dict) -> list:
    return [f.get("PitcherAcnt"), f.get("PitcherName"), f.get("Year"), f.get("KindCode"), f.get("GameSno"),
            (f.get("GameDate") or "")[:10], 1 if f.get("RoleType") == "先發" else 0,
            ip_to_outs(f.get("InningPitchedCnt")), f.get("EarnedRunCnt") or 0, f.get("HomeRunCnt") or 0,
            f.get("BasesONBallsCnt") or 0, f.get("HitBYPitchCnt") or 0, f.get("StrikeOutCnt") or 0,
            f.get("PlateAppearances") or 0, f.get("TeamNo") or ""]


# ---------------------------------------------------------------------------
# 轉換成系統格式
# ---------------------------------------------------------------------------
def game_info(r: list) -> dict:
    (yr, kind, sno, dt, vt, ht, vs, hs, vpa, vpn, hpa, hpn, field, status, result, stop) = (list(r) + [None] * 16)[:16]
    try:
        ko = datetime.strptime(dt[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=TW)
    except (TypeError, ValueError):
        ko = None
    # 官網欄位：PresentStatus=1 為有效場次（0=延賽/取消，之後會以同場次編號重排）；GameResult="0" 表示已完賽
    valid = str(status) == "1" and bool(vt) and bool(ht)
    finished = valid and str(result) == "0" and ko is not None and ko < datetime.now(TW) \
        and vs not in (None, "") and hs not in (None, "")
    return {"year": str(yr), "kind": kind, "sno": int(sno), "pk": game_pk(yr, kind, sno), "ko": ko,
            "away": team(vt), "home": team(ht), "away_score": vs, "home_score": hs,
            "away_sp_id": pid_of(vpa), "away_sp": vpn or "", "home_sp_id": pid_of(hpa), "home_sp": hpn or "",
            "field": field or "", "finished": bool(finished), "valid": valid, "stage": KINDS.get(kind, kind)}


def history_rows(raw_games: list[list]) -> list[dict]:
    rows = []
    for r in raw_games:
        g = game_info(r)
        if not g["finished"] or g["away_score"] in (None, "") or g["home_score"] in (None, ""):
            continue
        rows.append({"date": g["ko"].strftime("%Y-%m-%d"), "season": g["year"], "away": g["away"], "home": g["home"],
                     "away_score": float(g["away_score"]), "home_score": float(g["home_score"]), "source": "cpbl_official",
                     "game_pk": g["pk"], "away_sp_id": g["away_sp_id"], "home_sp_id": g["home_sp_id"],
                     "away_sp": g["away_sp"], "home_sp": g["home_sp"]})
    return rows


def pitching_rows(raw_logs: list[list], raw_games: list[list]) -> list[dict]:
    starters = {}
    for r in raw_games:
        g = game_info(r)
        starters[g["pk"]] = (g["away_sp_id"], g["home_sp_id"])
    out = []
    for r in raw_logs:
        (acnt, name, yr, kind, sno, date, gs, outs, er, hr, bb, hbp, k, pa, teamno) = (list(r) + [None] * 15)[:15]
        pid = pid_of(acnt)
        if not pid or not yr or sno in (None, ""):
            continue
        pk = game_pk(yr, kind, sno)
        a_sp, h_sp = starters.get(pk, (None, None))
        out.append({"pid": pid, "name": name or "", "season": str(yr), "date": (date or "")[:10], "game_pk": pk,
                    "is_home": 1 if pid == h_sp else 0, "gs": int(gs or 0), "outs": int(outs or 0),
                    "er": int(er or 0), "hr": int(hr or 0), "bb": int(bb or 0), "hbp": int(hbp or 0),
                    "k": int(k or 0), "bf": int(pa or 0)})
    return out


# ---------------------------------------------------------------------------
# 抓取歷史（在台灣的電腦上執行；約需數十分鐘，因為要放慢速度）
# ---------------------------------------------------------------------------
def fetch_history(years: list[int], kinds=("A", "E", "C"), progress=None) -> dict:
    import model
    import pitching
    c = CPBLClient()
    raw_games = []
    for y in years:
        for k in kinds:
            raw_games += c.season_games(y, k)
            if progress:
                progress(f"賽程 {y} {KINDS[k]}")
    pairs = set()
    for r in raw_games:
        g = game_info(r)
        for side in ("away", "home"):
            if g[f"{side}_sp_id"]:
                pairs.add((r[8] if side == "away" else r[10], g["year"], g["kind"]))
    raw_logs, failed = [], 0
    for i, (acnt, y, k) in enumerate(sorted(pairs)):
        try:
            raw_logs += c.pitcher_logs(acnt, int(y), k)
        except RuntimeError:
            failed += 1
        if progress and i % 20 == 0:
            progress(f"投手紀錄 {i}/{len(pairs)}")
    hist = history_rows(raw_games)
    model.save_history("CPBL", hist)
    pitching.save_logs(pitching_rows(raw_logs, raw_games), league="CPBL")
    return {"games": len(hist), "pitcher_seasons": len(pairs), "logs": len(raw_logs), "failed": failed}


def import_json(payload: dict) -> dict:
    """匯入瀏覽器收集的資料（cpbl_history_*.json）。"""
    import model
    import pitching
    raw_games, raw_logs = payload.get("games", []), payload.get("logs", [])
    hist = history_rows(raw_games)
    added = model.save_history("CPBL", hist)
    pr = pitching_rows(raw_logs, raw_games)
    added_logs = pitching.save_logs(pr, league="CPBL")
    return {"games": len(hist), "added_games": added, "logs": len(pr), "added_logs": added_logs}


# ---------------------------------------------------------------------------
# 即時同步：建立未來賽事、更新先發、填入賽果、補投手紀錄
# ---------------------------------------------------------------------------
def sync_cpbl(games: list[dict], days_ahead: int = 3, auto_create: bool = True) -> dict:
    import live_feed
    import pitching
    now = datetime.now(TW)
    c = CPBLClient()
    raw = []
    for k in ("A", "E", "C"):
        try:
            raw += c.season_games(now.year, k)
        except RuntimeError:
            if k == "A":
                raise
    rep = {"created": 0, "updated": 0, "results": 0, "new_history": [], "new_logs": 0}
    need_logs = set()
    for r in raw:
        g = game_info(r)
        if not g["valid"] or not g["ko"] or g["ko"] < now - timedelta(days=4) or g["ko"] > now + timedelta(days=days_ahead):
            continue
        x = live_feed.find_game(games, "CPBL", g["away"], g["home"], g["ko"])
        if x is None:
            if g["finished"] or not auto_create:
                continue
            x = live_feed._new_game(f"CPBL_{g['pk']}", "CPBL", "baseball", g["ko"], g["away"], g["home"],
                                    stage=g["stage"], venue=g["field"], source="cpbl_official",
                                    away_starter=g["away_sp"] or "官方未公布", home_starter=g["home_sp"] or "官方未公布")
            games.append(x)
            rep["created"] += 1
        changed = False
        x["game_pk"] = g["pk"]
        for side in ("away", "home"):
            name, pid = g[f"{side}_sp"], g[f"{side}_sp_id"]
            if name and x.get(f"{side}_starter") != name:
                x[f"{side}_starter"] = name
                changed = True
            if name:
                x[f"{side}_sp_id"], x[f"{side}_sp_src"] = pid, name
                if pid:
                    need_logs.add((r[8] if side == "away" else r[10], g["year"], g["kind"]))
        if g["finished"] and not x.get("result"):
            a, h = float(g["away_score"]), float(g["home_score"])
            x["result"] = "home" if h > a else ("away" if a > h else "draw")
            x["score"] = f"{int(a)}-{int(h)}"
            rep["results"] += 1
        elif changed:
            rep["updated"] += 1
    rep["new_history"] = history_rows(raw)  # 整季完賽（save_history 會去重）
    # 補投手紀錄（近期先發投手）
    raw_logs = []
    for acnt, y, k in sorted(need_logs):
        try:
            raw_logs += c.pitcher_logs(acnt, int(y), k)
        except RuntimeError:
            pass
    if raw_logs:
        rep["new_logs"] = pitching.save_logs(pitching_rows(raw_logs, raw), league="CPBL")
    return rep


# ---------------------------------------------------------------------------
# 慢速補齊缺少的投手逐場紀錄（每次同步補一點，避免被官網封鎖）
# ---------------------------------------------------------------------------
def missing_pairs() -> list[tuple[str, str, str]]:
    import model
    import pitching
    have = {(r["pid"], r["season"], str(r["game_pk"])[4]) for r in pitching.load_logs("CPBL") if r["game_pk"]}
    inv = {v: k for k, v in KIND_NUM.items()}
    need = set()
    for r in model.load_history("CPBL"):
        pk = r.get("game_pk")
        if not pk:
            continue
        kd = str(pk)[4]
        for side in ("away", "home"):
            pid = r.get(f"{side}_sp_id")
            if pid and (pid, r["season"], kd) not in have:
                need.add((f"{pid:010d}", r["season"], inv.get(int(kd), "A")))
    return sorted(need, key=lambda x: (x[1], x[0]), reverse=True)  # 近年優先


def backfill_logs(limit: int = 25, delay: float = 2.0) -> dict:
    import pitching
    import model
    todo = missing_pairs()
    if not todo:
        return {"fetched": 0, "remaining": 0}
    c = CPBLClient(delay=delay)
    raw_logs, done = [], 0
    for acnt, y, k in todo[:limit]:
        try:
            raw_logs += c.pitcher_logs(acnt, int(y), k)
            done += 1
        except RuntimeError:
            break  # 被擋就先停，下次再補
    rows = pitching_rows(raw_logs, [])
    # 用歷史資料的先發對照判斷主客
    starters = {r["game_pk"]: (r.get("away_sp_id"), r.get("home_sp_id")) for r in model.load_history("CPBL")}
    for r in rows:
        a_sp, h_sp = starters.get(r["game_pk"], (None, None))
        r["is_home"] = 1 if r["pid"] == h_sp else 0
    added = pitching.save_logs(rows, league="CPBL") if rows else 0
    return {"fetched": done, "added": added, "remaining": len(todo) - done}


# ---------------------------------------------------------------------------
# 中職進階數據網站（stats.cpbl.com.tw）：當季每場 box score（不限台灣網路、較不易被擋）
# ---------------------------------------------------------------------------
STATS_API = "https://stats.cpbl.com.tw/api/proxy/v1"


def stats_box_rows(year, kind, sno) -> list[dict]:
    """取得單場雙方所有投手成績，轉成 pitching 格式。"""
    req = urllib.request.Request(f"{STATS_API}/games/{year}-{kind}-{int(sno)}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        g = json.loads(r.read().decode("utf-8"))["Data"]["Game"]
    pk = game_pk(year, kind, sno)
    date = (g.get("PreExeDate") or "")[:10]
    out = []
    for side, is_home in (("Visiting", 0), ("Home", 1)):
        for p in g.get(side, {}).get("Pitchers", []) or []:
            pid = pid_of(p.get("PitcherAcnt"))
            if not pid:
                continue
            out.append({"pid": pid, "name": p.get("PitcherName") or "", "season": str(year), "date": date,
                        "game_pk": pk, "is_home": is_home, "gs": 1 if p.get("RoleType") == "先發" else 0,
                        "outs": int(p.get("InningPitchedCnt") or 0) * 3 + int(p.get("InningPitchedDiv3Cnt") or 0),
                        "er": int(p.get("EarnedRunCnt") or 0), "hr": int(p.get("HomeRunCnt") or 0),
                        "bb": int(p.get("BasesONBallsCnt") or 0), "hbp": int(p.get("HitBYPitchCnt") or 0),
                        "k": int(p.get("StrikeOutCnt") or 0), "bf": int(p.get("PlateAppearances") or 0)})
    return out


def sync_stats_boxes(limit: int = 40) -> dict:
    """補上已完賽、但還沒有投手成績的當季比賽（stats.cpbl.com.tw 只有 2026 起的資料）。"""
    import model
    import pitching
    have = {r["game_pk"] for r in pitching.load_logs("CPBL") if r["gs"] == 1}
    inv = {v: k for k, v in KIND_NUM.items()}
    todo = [r["game_pk"] for r in model.load_history("CPBL")
            if r.get("game_pk") and int(r["season"]) >= 2026 and r["game_pk"] not in have]
    rows, done = [], 0
    for pk in todo[-limit:]:
        s_ = str(pk)
        try:
            rows += stats_box_rows(s_[:4], inv.get(int(s_[4]), "A"), int(s_[5:]))
            done += 1
        except Exception:
            continue
        time.sleep(0.3)
    added = pitching.save_logs(rows, league="CPBL") if rows else 0
    return {"fetched": done, "added": added, "remaining": len(todo) - done}


def sync_cpbl_stats(games: list[dict], days_ahead: int = 4, auto_create: bool = True) -> dict:
    """只用 stats.cpbl.com.tw（海外也能連）同步中職：建立未來賽事、填賽果、補當季 box score。
    ※ 這個網站在賽前不提供預計先發，先發需由使用者自行輸入或等官網同步。"""
    import live_feed
    import model
    import pitching
    now = datetime.now(TW)
    months = {(now.year, now.month), ((now + timedelta(days=days_ahead)).year, (now + timedelta(days=days_ahead)).month),
              ((now - timedelta(days=5)).year, (now - timedelta(days=5)).month)}
    sched = []
    for y, m in sorted(months):
        for k in ("A", "E", "C"):
            try:
                req = urllib.request.Request(f"{STATS_API}/games/schedule?year={y}&kindCode={k}&month={m}",
                                             headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    sched += json.loads(r.read().decode("utf-8"))["Data"]["Games"] or []
            except Exception:
                continue
            time.sleep(0.3)
    hist_pk = {r.get("game_pk") for r in model.load_history("CPBL")}
    rep = {"created": 0, "updated": 0, "results": 0, "new_history": [], "new_logs": 0}
    new_logs = []
    for x in sched:
        try:
            y, k, sno = x["GameId"].split("-")
            ko = datetime.strptime(x["PreExeDate"][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=TW)
        except Exception:
            continue
        status = x.get("GameStatus")
        vt = team(((x.get("Visiting") or {}).get("Team") or {}).get("Name", ""))
        ht = team(((x.get("Home") or {}).get("Team") or {}).get("Name", ""))
        if not vt or not ht or status in ("POSTPONED", "CANCELED", "CANCELLED"):
            continue
        pk = game_pk(y, k, sno)
        g = live_feed.find_game(games, "CPBL", vt, ht, ko)
        if status == "FINISHED":
            vs, hs = (x.get("Visiting") or {}).get("Score"), (x.get("Home") or {}).get("Score")
            if vs is None or hs is None:
                continue
            if pk not in hist_pk:
                try:
                    box = stats_box_rows(y, k, sno)
                except Exception:
                    box = []
                new_logs += box
                sp = {b["is_home"]: b for b in box if b["gs"] == 1}
                rep["new_history"].append({
                    "date": ko.strftime("%Y-%m-%d"), "season": y, "away": vt, "home": ht,
                    "away_score": float(vs), "home_score": float(hs), "source": "cpbl_stats", "game_pk": pk,
                    "away_sp_id": sp.get(0, {}).get("pid"), "home_sp_id": sp.get(1, {}).get("pid"),
                    "away_sp": sp.get(0, {}).get("name", ""), "home_sp": sp.get(1, {}).get("name", "")})
                time.sleep(0.3)
            if g and not g.get("result"):
                g["result"] = "home" if hs > vs else ("away" if vs > hs else "draw")
                g["score"] = f"{int(vs)}-{int(hs)}"
                rep["results"] += 1
            continue
        # 尚未開打
        if ko < now or ko > now + timedelta(days=days_ahead):
            continue
        if g is None and auto_create:
            g = live_feed._new_game(f"CPBL_{pk}", "CPBL", "baseball", ko, vt, ht, stage=KINDS.get(k, k),
                                    venue=((x.get("Field") or {}).get("Abbe") or ""), source="cpbl_stats",
                                    away_starter="官方未公布", home_starter="官方未公布")
            g["game_pk"] = pk
            games.append(g)
            rep["created"] += 1
        elif g is not None:
            changed = False
            if g.get("date") != ko.strftime("%Y-%m-%d") or g.get("time") != ko.strftime("%H:%M"):
                g["date"], g["time"] = ko.strftime("%Y-%m-%d"), ko.strftime("%H:%M")
                changed = True
            g["game_pk"] = pk
            rep["updated"] += int(changed)
    if new_logs:
        rep["new_logs"] = pitching.save_logs(new_logs, league="CPBL")
    return rep
