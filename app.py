# -*- coding: utf-8 -*-
"""
BallMetrics AI - 運動彩券量化決策與統計分析系統（v3：Elo 勝率模型 + 即時資料同步）

啟動：python app.py   → 瀏覽器開 http://127.0.0.1:8000
資料：data/games.json（賽事）、data/bets.json（下注紀錄）、data/settings.json（參數）
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import engine
import access
import cpbl_feed
import live_feed
import model

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
STATIC = BASE / "static"
DATA.mkdir(exist_ok=True)

GAMES_FILE = DATA / "games.json"
BETS_FILE = DATA / "bets.json"
SETTINGS_FILE = DATA / "settings.json"
STATUS_FILE = DATA / "feed_status.json"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# JSON 儲存（原子寫入，避免存檔中斷造成檔案損毀）
# ---------------------------------------------------------------------------
def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_settings() -> dict:
    return {**engine.DEFAULT_SETTINGS, **_load(SETTINGS_FILE, {})}


def now_str() -> str:
    return datetime.now(engine.TW).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
class Report(BaseModel):
    why_bet: str = ""
    pitcher_eval: str = ""
    batting_eval: str = ""
    defense_eval: str = ""
    team_winrate: str = ""
    home_away: str = ""


class Game(BaseModel):
    id: Optional[str] = None
    league: str
    sport: Literal["baseball", "basketball", "soccer", "other"] = "baseball"
    stage: str = ""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    venue: str = ""
    away_team: str
    away_starter: str = ""
    home_team: str
    home_starter: str = ""
    away_odds: Optional[float] = Field(None, gt=1)
    home_odds: Optional[float] = Field(None, gt=1)
    draw_odds: Optional[float] = None
    model_source: Literal["manual", "auto"] = "manual"
    model_p_away: Optional[float] = Field(None, ge=0, le=100)
    model_p_home: Optional[float] = Field(None, ge=0, le=100)
    model_p_draw: Optional[float] = Field(None, ge=0, le=100)
    open_away_odds: Optional[float] = None
    open_home_odds: Optional[float] = None
    result: Optional[Literal["away", "home", "draw"]] = None
    score: str = ""
    report: Report = Report()


class GamePatch(BaseModel):
    away_odds: Optional[float] = Field(None, gt=1)
    home_odds: Optional[float] = Field(None, gt=1)
    draw_odds: Optional[float] = None
    model_p_away: Optional[float] = Field(None, ge=0, le=100)
    model_p_home: Optional[float] = Field(None, ge=0, le=100)
    model_p_draw: Optional[float] = Field(None, ge=0, le=100)
    result: Optional[Literal["away", "home", "draw", ""]] = None
    score: Optional[str] = None
    model_source: Optional[Literal["manual", "auto"]] = None


class Bet(BaseModel):
    game_id: Optional[str] = None
    league: str = ""
    match: str = ""
    selection: str
    side: Optional[Literal["away", "home", "draw"]] = None
    odds: float = Field(..., gt=1)
    stake: float = Field(..., gt=0)
    model_p: Optional[float] = None
    ev_at_bet: Optional[float] = None
    bet_type: Literal["single", "parlay"] = "single"
    note: str = ""


class BetPatch(BaseModel):
    status: Optional[Literal["pending", "won", "lost", "push", "void"]] = None
    closing_odds: Optional[float] = Field(None, gt=1)
    note: Optional[str] = None


class ParlayLeg(BaseModel):
    odds: float = Field(..., gt=1)
    p: float = Field(..., gt=0, le=100)
    fair_p: Optional[float] = None


class Parlay(BaseModel):
    legs: list[ParlayLeg]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="BallMetrics AI Engine")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
PORT = int(os.environ.get("BM_PORT", "8000"))

# ---------------------------------------------------------------------------
# 手機連線保護：本機免登入；區網有設 PIN 才要登入；遠端通道一律要登入
# ---------------------------------------------------------------------------
OPEN_PATHS = ("/login", "/api/login", "/static/icons/", "/static/login", "/manifest.webmanifest", "/sw.js")


@app.middleware("http")
async def guard(request: Request, call_next):
    kind = access.classify(request.client.host if request.client else None, request.headers)
    request.state.kind = kind
    path = request.url.path
    if access.needs_login(kind) and not path.startswith(OPEN_PATHS) \
            and not access.valid_token(request.cookies.get(access.COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "請先登入"}, status_code=401)
        return RedirectResponse("/login")
    resp = await call_next(request)
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


class LoginReq(BaseModel):
    pin: str


@app.post("/api/login")
def login(req: LoginReq, request: Request):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    if access.login_blocked(ip):
        raise HTTPException(429, "錯誤次數過多，請 10 分鐘後再試")
    if not access.pin_set():
        raise HTTPException(403, "尚未設定 PIN，請先在電腦上的「參數設定 → 手機使用」設定")
    if not access.check_pin(req.pin):
        access.record_fail(ip)
        raise HTTPException(401, "PIN 錯誤")
    resp = JSONResponse({"status": "success"})
    secure = request.headers.get("x-forwarded-proto") == "https" or "cf-visitor" in request.headers
    resp.set_cookie(access.COOKIE, access.make_token(), max_age=access.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=secure)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"status": "success"})
    resp.delete_cookie(access.COOKIE)
    return resp


@app.get("/api/access")
def access_info(request: Request):
    lan = [f"http://{ip}:{PORT}" for ip in access.lan_ips()]
    d = access._load()
    return {"status": "success", "kind": request.state.kind, "pin_set": access.pin_set(), "lan_urls": lan,
            "remote_enabled": bool(d.get("remote_enabled")), "tunnel": access.tunnel.status()}


class PinReq(BaseModel):
    pin: str = ""
    current: str = ""
    clear: bool = False


@app.post("/api/access/pin")
def access_pin(req: PinReq, request: Request):
    # 已有 PIN 時，非本機修改需輸入目前 PIN；清除 PIN 只能在電腦本機操作
    if access.pin_set() and request.state.kind != "local" and not access.check_pin(req.current):
        raise HTTPException(401, "目前的 PIN 不正確")
    if req.clear:
        if request.state.kind != "local":
            raise HTTPException(403, "清除 PIN 只能在電腦上操作")
        access.tunnel.stop()
        access.clear_pin()
        d = access._load(); d["remote_enabled"] = False; access._save(d)
        return {"status": "success", "pin_set": False}
    try:
        access.set_pin(req.pin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"status": "success", "pin_set": True})
    if request.state.kind != "local":  # 換 PIN 後舊 Cookie 失效，幫目前裝置重新登入
        resp.set_cookie(access.COOKIE, access.make_token(), max_age=access.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
    return resp


class TunnelReq(BaseModel):
    enable: bool


@app.post("/api/access/tunnel")
async def access_tunnel(req: TunnelReq):
    d = access._load()
    if req.enable:
        st = await asyncio.to_thread(access.tunnel.start, PORT)
        d["remote_enabled"] = st["running"]
    else:
        st = access.tunnel.stop()
        d["remote_enabled"] = False
    access._save(d)
    return {"status": "success", "tunnel": st}


# ---------------------------------------------------------------------------
# 勝率模型整合
# ---------------------------------------------------------------------------
_model_cache: dict[str, tuple[float, dict]] = {}


def get_model(league: str) -> dict | None:
    p = model.MODEL_DIR / f"{league.upper()}.json"
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    c = _model_cache.get(league)
    if c and c[0] == mt:
        return c[1]
    m = model.load_model(league)
    if m:
        _model_cache[league] = (mt, m)
    return m


def resolve_model(game: dict, settings: dict) -> dict:
    """計算 Elo 與國際盤共識機率；若賽事設為自動，依設定混合後填入模型勝率。"""
    g = dict(game)
    lg = (g.get("league") or "").upper()
    sp_kw = {}
    if lg in model.SP_LEAGUES:
        for side in ("away", "home"):
            name = g.get(f"{side}_starter", "")
            sp_kw[f"{side}_sp"] = name
            # 只有名字仍是同步寫入的那位時才用官方 ID（使用者手動改名就改用名字比對）
            if g.get(f"{side}_sp_id") and name == g.get(f"{side}_sp_src"):
                sp_kw[f"{side}_sp_id"] = g[f"{side}_sp_id"]
    elo = model.predict(lg, g.get("away_team", ""), g.get("home_team", ""), get_model(lg), **sp_kw)
    mk = g.get("market") or None
    det = {"source": g.get("model_source", "manual"), "mode": settings["model_mode"],
           "elo_home": round(elo["p_home"] * 100, 2) if elo else None,
           "elo_away": round(elo["p_away"] * 100, 2) if elo else None,
           "r_home": elo and elo["r_home"], "r_away": elo and elo["r_away"],
           "market_home": mk and mk.get("p_home"), "market_away": mk and mk.get("p_away"),
           "books": mk and mk.get("books"), "pinnacle": mk and mk.get("pinnacle"),
           "best_home": mk and mk.get("best_home"), "best_away": mk and mk.get("best_away"),
           "market_updated": mk and mk.get("updated_at"), "used": None,
           "team_only_home": elo and round(elo["p_home_team_only"] * 100, 2),
           "sp_elo_adj": elo and elo.get("sp_elo_adj"), "sp": None}
    if elo and (elo.get("sp_away") or elo.get("sp_home")):
        import pitching
        det["sp"] = {}
        det["lg_era"] = round(get_model(lg)["sp"]["lg_era"], 2)
        for side in ("away", "home"):
            v = elo.get(f"sp_{side}")
            if v:
                v = {**v, "recent": pitching.recent_starts(v["pid"], league=lg) if v["pid"] else None}
            det["sp"][side] = v
    if g.get("model_source") == "auto":
        w = float(settings["elo_weight"])
        mode = settings["model_mode"]
        eh = elo["p_home"] * 100 if elo else None
        mh = mk.get("p_home") if mk else None
        three_way = bool(g.get("draw_odds")) and mk and mk.get("p_draw")
        if three_way:
            g["model_p_home"], g["model_p_away"], g["model_p_draw"] = mk["p_home"], mk["p_away"], mk["p_draw"]
            det["used"] = "國際盤共識（三選項）"
        else:
            ph = None
            if mode == "elo":
                ph, det["used"] = (eh, "Elo") if eh is not None else (mh, "國際盤共識（無 Elo 資料）")
            elif mode == "market":
                ph, det["used"] = (mh, "國際盤共識") if mh is not None else (eh, "Elo（無國際盤資料）")
            else:
                if eh is not None and mh is not None:
                    ph, det["used"] = w * eh + (1 - w) * mh, f"混合：Elo {w:.0%} + 國際盤 {1 - w:.0%}"
                elif eh is not None:
                    ph, det["used"] = eh, "Elo（無國際盤資料）"
                elif mh is not None:
                    ph, det["used"] = mh, "國際盤共識（無 Elo 資料）"
            if ph is not None:
                g["model_p_home"] = round(ph, 2)
                g["model_p_away"] = round(100 - ph, 2)
    g["model_detail"] = det
    return g


def _record_result(g: dict) -> list[dict]:
    """賽事填入比分後，寫入該聯盟歷史資料，讓 Elo 持續學習。比分格式：客-主，例如 3-2。"""
    try:
        if not g.get("result") or not g.get("score"):
            return []
        if (g.get("league") or "").upper() == "MLB":
            return []  # MLB 歷史由官方資料同步，避免日期（美/台時差）造成重複
        a, h = [float(x) for x in str(g["score"]).replace("：", "-").replace(":", "-").split("-")[:2]]
        row = {"date": g["date"], "season": model.season_of(g["date"], g["league"]), "away": model.zh(g["away_team"]),
               "home": model.zh(g["home_team"]), "away_score": a, "home_score": h, "source": "app"}
        model.save_history(g["league"], [row])
        return [row]
    except Exception:
        return []


def _analyzed_all() -> tuple[list[dict], dict, dict]:
    settings = get_settings()
    games = _load(GAMES_FILE, [])
    bets = _load(BETS_FILE, [])
    stats = engine.bet_stats(bets, float(settings["bankroll"]))
    limit = float(settings["bankroll"]) * float(settings["daily_loss_limit_pct"]) / 100
    risk_block = stats["today_profit"] <= -limit if limit > 0 else False
    analyzed = [engine.analyze_game(resolve_model(g, settings), settings, stats["current_bankroll"], risk_block)
                for g in games]
    analyzed.sort(key=lambda g: (g["status"] == "settled", g["date"], g["time"]))
    return analyzed, settings, {**stats, "risk_block": risk_block}


@app.get("/api/analysis")
def get_analysis():
    analyzed, settings, stats = _analyzed_all()
    return {
        "status": "success",
        "data": analyzed,
        "settings": public_settings(),
        "feed": _load(STATUS_FILE, {}),
        "bankroll": stats["current_bankroll"],
        "risk_block": stats["risk_block"],
        "generated_at": now_str(),
    }


# ---- 賽事 CRUD ----
@app.post("/api/games")
def create_game(game: Game):
    with _lock:
        games = _load(GAMES_FILE, [])
        g = game.model_dump()
        g["id"] = g.get("id") or f"{g['league']}_{g['date'].replace('-', '')}_{uuid.uuid4().hex[:6]}"
        if any(x["id"] == g["id"] for x in games):
            raise HTTPException(400, "賽事 ID 重複")
        # 第一次輸入的賠率自動記為開盤賠率，之後可觀察盤口變化
        g["open_away_odds"] = g.get("open_away_odds") or g["away_odds"]
        g["open_home_odds"] = g.get("open_home_odds") or g["home_odds"]
        _record_result(g)
        games.append(g)
        _save(GAMES_FILE, games)
    return {"status": "success", "id": g["id"]}


@app.put("/api/games/{gid}")
def replace_game(gid: str, game: Game):
    with _lock:
        games = _load(GAMES_FILE, [])
        for i, x in enumerate(games):
            if x["id"] == gid:
                g = {**x, **game.model_dump()}  # 保留 market 等同步欄位
                g["id"] = gid
                games[i] = g
                _record_result(g)
                _save(GAMES_FILE, games)
                return {"status": "success"}
    raise HTTPException(404, "找不到賽事")


@app.patch("/api/games/{gid}")
def patch_game(gid: str, patch: GamePatch):
    with _lock:
        games = _load(GAMES_FILE, [])
        for x in games:
            if x["id"] == gid:
                upd = patch.model_dump(exclude_unset=True)
                for side in ("away", "home"):
                    k = f"{side}_odds"
                    if k in upd and not x.get(f"open_{k}"):
                        x[f"open_{k}"] = x.get(k)
                if upd.get("result") == "":
                    upd["result"] = None
                x.update(upd)
                _record_result(x)
                _save(GAMES_FILE, games)
                return {"status": "success"}
    raise HTTPException(404, "找不到賽事")


@app.delete("/api/games/{gid}")
def delete_game(gid: str):
    with _lock:
        games = _load(GAMES_FILE, [])
        new = [x for x in games if x["id"] != gid]
        if len(new) == len(games):
            raise HTTPException(404, "找不到賽事")
        _save(GAMES_FILE, new)
    return {"status": "success"}


# ---- 下注紀錄 ----
@app.get("/api/bets")
def list_bets():
    bets = _load(BETS_FILE, [])
    for b in bets:
        b["profit"] = round(engine.settle_profit(b), 0)
    return {"status": "success", "data": sorted(bets, key=lambda b: b.get("created_at", ""), reverse=True)}


@app.post("/api/bets")
def create_bet(bet: Bet):
    with _lock:
        bets = _load(BETS_FILE, [])
        b = bet.model_dump()
        b.update({"id": uuid.uuid4().hex[:10], "status": "pending", "created_at": now_str(),
                  "settled_at": None, "closing_odds": None})
        bets.append(b)
        _save(BETS_FILE, bets)
    return {"status": "success", "id": b["id"]}


@app.patch("/api/bets/{bid}")
def patch_bet(bid: str, patch: BetPatch):
    with _lock:
        bets = _load(BETS_FILE, [])
        for b in bets:
            if b["id"] == bid:
                upd = patch.model_dump(exclude_unset=True)
                if "status" in upd:
                    upd["settled_at"] = None if upd["status"] == "pending" else now_str()
                b.update(upd)
                _save(BETS_FILE, bets)
                return {"status": "success"}
    raise HTTPException(404, "找不到下注紀錄")


@app.delete("/api/bets/{bid}")
def delete_bet(bid: str):
    with _lock:
        bets = _load(BETS_FILE, [])
        _save(BETS_FILE, [b for b in bets if b["id"] != bid])
    return {"status": "success"}


def _auto_settle_locked() -> int:
    games = {g["id"]: g for g in _load(GAMES_FILE, [])}
    n = 0
    if True:
        bets = _load(BETS_FILE, [])
        for b in bets:
            g = games.get(b.get("game_id") or "")
            if b["status"] != "pending" or b.get("bet_type") != "single" or not g or not g.get("result"):
                continue
            side = b.get("side")
            if side not in engine.SIDES:
                continue
            b["status"] = "won" if g["result"] == side else "lost"
            b["settled_at"] = now_str()
            n += 1
        if n:
            _save(BETS_FILE, bets)
    return n


@app.post("/api/bets/auto-settle")
def auto_settle():
    """依賽事已填入的賽果，自動結算對應的單場下注。"""
    with _lock:
        n = _auto_settle_locked()
    return {"status": "success", "settled": n}


@app.get("/api/bets.csv")
def export_bets():
    bets = _load(BETS_FILE, [])
    buf = io.StringIO()
    buf.write("﻿")  # 讓 Excel 正確顯示中文
    cols = ["created_at", "league", "match", "selection", "odds", "stake", "model_p", "ev_at_bet",
            "status", "closing_odds", "settled_at", "profit", "note"]
    w = csv.writer(buf)
    w.writerow(["下注時間", "聯盟", "賽事", "選項", "賠率", "注金", "模型勝率%", "下注時EV%",
                "狀態", "收盤賠率", "結算時間", "損益", "備註"])
    for b in bets:
        b["profit"] = round(engine.settle_profit(b), 0)
        w.writerow([b.get(c, "") if b.get(c) is not None else "" for c in cols])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=bets.csv"})


# ---- 統計 / 校準 / 回測 ----
@app.get("/api/stats")
def stats():
    analyzed, settings, st = _analyzed_all()
    return {
        "status": "success",
        "bets": st,
        "calibration": engine.calibration(analyzed),
        "backtest": engine.backtest(analyzed, settings),
        "settings": public_settings(),
    }


# ---- 串關 ----
@app.post("/api/parlay")
def calc_parlay(p: Parlay):
    if len(p.legs) < 2:
        raise HTTPException(400, "串關至少需要 2 場")
    return {"status": "success", "data": engine.parlay([l.model_dump() for l in p.legs], get_settings())}


# ---- 設定 ----
@app.get("/api/settings")
def read_settings():
    return {"status": "success", "data": public_settings()}


class SettingsPatch(BaseModel):
    bankroll: Optional[float] = Field(None, gt=0)
    kelly_fraction: Optional[float] = Field(None, gt=0, le=1)
    min_ev: Optional[float] = Field(None, ge=0, le=50)
    min_edge: Optional[float] = Field(None, ge=0, le=50)
    max_stake_pct: Optional[float] = Field(None, gt=0, le=100)
    min_odds: Optional[float] = Field(None, gt=1)
    max_odds: Optional[float] = Field(None, gt=1)
    devig_method: Optional[Literal["proportional", "power"]] = None
    stake_round: Optional[int] = Field(None, ge=1)
    daily_loss_limit_pct: Optional[float] = Field(None, ge=0, le=100)
    model_mode: Optional[Literal["blend", "elo", "market"]] = None
    elo_weight: Optional[float] = Field(None, ge=0, le=1)
    auto_sync: Optional[bool] = None
    sync_interval_min: Optional[int] = Field(None, ge=15, le=1440)
    odds_api_key: Optional[str] = None
    odds_regions: Optional[str] = None
    odds_sports: Optional[list[str]] = None
    mlb_auto_create: Optional[bool] = None
    odds_auto_create: Optional[bool] = None
    fetch_scores: Optional[bool] = None
    auto_retrain: Optional[bool] = None


def public_settings() -> dict:
    s = get_settings()
    k = s.get("odds_api_key") or ""
    s["odds_api_key"] = (k[:4] + "…" + k[-4:]) if len(k) > 8 else ("…" if k else "")
    s["has_odds_key"] = bool(k)
    return s


@app.put("/api/settings")
def update_settings(patch: SettingsPatch):
    with _lock:
        s = get_settings()
        upd = patch.model_dump(exclude_unset=True, exclude_none=True)
        if "…" in (upd.get("odds_api_key") or ""):
            upd.pop("odds_api_key")  # 前端顯示的是遮罩，沒改就不覆蓋
        s.update(upd)
        _save(SETTINGS_FILE, s)
    return {"status": "success", "data": public_settings()}


# ---------------------------------------------------------------------------
# 模型中心
# ---------------------------------------------------------------------------
@app.get("/api/models")
def list_models():
    return {"status": "success", "data": model.list_models()}


@app.post("/api/models/{league}/train")
def train_model(league: str):
    try:
        m = model.train(league)
    except ValueError as e:
        raise HTTPException(400, str(e))
    m.pop("ratings", None)
    return {"status": "success", "data": m}


class FetchReq(BaseModel):
    seasons: list[int] = Field(default_factory=lambda: list(range(datetime.now().year - 5, datetime.now().year + 1)))


@app.post("/api/models/MLB/fetch")
def fetch_mlb(req: FetchReq):
    try:
        info = model.fetch_mlb_full(req.seasons)
    except Exception as e:
        raise HTTPException(502, f"無法連線 MLB 官方資料：{e}")
    m = model.train("MLB")
    return {"status": "success", **info, "validation": m["validation"]}


class ImportReq(BaseModel):
    csv_text: str
    train: bool = True


@app.post("/api/history/{league}/import")
def import_history(league: str, req: ImportReq):
    league = league.upper()
    rows = model.parse_csv_text(req.csv_text, league)
    if not rows:
        raise HTTPException(400, "沒有讀到有效資料，請確認欄位：date,away,home,away_score,home_score")
    added = model.save_history(league, rows)
    out = {"status": "success", "parsed": len(rows), "added": added}
    if req.train:
        try:
            m = model.train(league)
            out["validation"] = m["validation"]
        except ValueError as e:
            out["train_error"] = str(e)
    return out


class CpblImport(BaseModel):
    payload: dict


@app.post("/api/cpbl/import-json")
def cpbl_import_json(req: CpblImport):
    """匯入在瀏覽器收集的中職官網資料（cpbl_history_*.json）。"""
    info = cpbl_feed.import_json(req.payload)
    try:
        m = model.train("CPBL")
        info["validation"] = m["validation"]
    except ValueError as e:
        info["train_error"] = str(e)
    return {"status": "success", **info}


class CpblFetch(BaseModel):
    years: list[int] = Field(default_factory=lambda: list(range(2019, datetime.now().year + 1)))


@app.post("/api/models/CPBL/fetch")
def cpbl_fetch(req: CpblFetch):
    """直接從中職官網抓歷史（需台灣網路，為避免被封鎖會刻意放慢，約 10~20 分鐘）。"""
    try:
        info = cpbl_feed.fetch_history(req.years)
    except Exception as e:
        raise HTTPException(502, f"無法連線中職官網：{e}")
    m = model.train("CPBL")
    return {"status": "success", **info, "validation": m["validation"]}


@app.get("/api/history/{league}.csv")
def download_history(league: str):
    p = model.hist_path(league)
    if not p.exists():
        raise HTTPException(404, "尚無歷史資料")
    return FileResponse(p, media_type="text/csv", filename=f"{league.upper()}_history.csv")


# ---------------------------------------------------------------------------
# 即時資料同步（背景排程 + 手動）
# ---------------------------------------------------------------------------
_sync_lock = threading.Lock()


def run_sync(force_odds: bool = False) -> dict:
    if not _sync_lock.acquire(blocking=False):
        return _load(STATUS_FILE, {})
    try:
        s = get_settings()
        st = _load(STATUS_FILE, {})
        now = datetime.now(engine.TW)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        errors: list[str] = []
        new_hist: dict[str, list] = {}
        new_logs: set[str] = set()
        summary = {"created": 0, "updated": 0, "results": 0}
        with _lock:
            games = _load(GAMES_FILE, [])
            # 1) MLB 官方（免費）
            try:
                r = live_feed.sync_mlb(games, auto_create=s["mlb_auto_create"])
                for k in summary:
                    summary[k] += r.get(k, 0)
                new_hist.setdefault("MLB", []).extend(r["new_history"])
                if r.get("new_logs"):
                    new_logs.add("MLB")
                if r.get("pitch_error"):
                    errors.append("投手紀錄：" + r["pitch_error"])
                st["mlb_last"] = stamp
            except Exception as e:
                errors.append(f"MLB 官方資料：{e}")
            # 1b) 中職官網（需台灣網路；預設每 2 小時一次，避免被官網封鎖）
            last_c = st.get("cpbl_last")
            due_c = force_odds or not last_c or (now - datetime.strptime(last_c, "%Y-%m-%d %H:%M:%S")
                                                  .replace(tzinfo=engine.TW)).total_seconds() >= 120 * 60
            if s.get("cpbl_sync", True) and due_c:
                try:
                    r = cpbl_feed.sync_cpbl(games, auto_create=s.get("cpbl_auto_create", True))
                    for k in summary:
                        summary[k] += r.get(k, 0)
                    new_hist.setdefault("CPBL", []).extend(r["new_history"])
                    if r.get("new_logs"):
                        new_logs.add("CPBL")
                    st["cpbl_last"] = stamp
                except Exception as e:
                    errors.append(f"中職官網：{e}")
                try:  # 當季投手成績：中職進階數據網站（不易被擋）
                    sb = cpbl_feed.sync_stats_boxes()
                    st["cpbl_stats_box"] = sb
                    if sb.get("added"):
                        new_logs.add("CPBL")
                except Exception as e:
                    errors.append(f"中職進階數據：{e}")
                try:  # 慢慢補齊歷史投手紀錄
                    bf = cpbl_feed.backfill_logs(limit=25)
                    st["cpbl_backfill"] = bf
                    if bf.get("added"):
                        new_logs.add("CPBL")
                except Exception as e:
                    errors.append(f"中職投手紀錄補齊：{e}")
            # 2) The Odds API（依間隔或手動）
            key = s.get("odds_api_key")
            last = st.get("odds_last")
            due = force_odds or not last or \
                (now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=engine.TW)).total_seconds() \
                >= s["sync_interval_min"] * 60
            if key and due:
                r = live_feed.sync_odds_api(games, key, s["odds_sports"], s["odds_regions"], s["odds_auto_create"])
                summary["created"] += r["created"]
                summary["updated"] += r["updated"]
                errors += r["errors"]
                st["odds_last"] = stamp
                st["odds_events"] = r["events"]
                if r["quota_remaining"] is not None:
                    st["quota_remaining"] = r["quota_remaining"]
                    st["quota_used"] = r["quota_used"]
                if s["fetch_scores"]:
                    r2 = live_feed.sync_scores_odds_api(games, key, s["odds_sports"])
                    summary["results"] += r2["results"]
                    errors += r2["errors"]
                    for lg, rows in r2["new_history"].items():
                        new_hist.setdefault(lg, []).extend(rows)
            _save(GAMES_FILE, games)
            settled = _auto_settle_locked() if summary["results"] else 0
        # 3) 新賽果寫入歷史並重新訓練
        retrained = []
        for lg in set(new_hist) | new_logs:
            rows = new_hist.get(lg, [])
            added = model.save_history(lg, rows) if rows else 0
            if (added or lg in new_logs) and s["auto_retrain"]:
                try:
                    prev = model.load_model(lg)
                    # 已有模型：沿用參數快速更新；中職資料量小，或投手調整尚未啟用時重新搜尋參數
                    model.train(lg, search=prev is None or lg == "CPBL" or (lg in model.SP_LEAGUES and not prev.get("sp")))
                    retrained.append(lg)
                except ValueError:
                    pass
        st.update({"last_run": stamp, "last_summary": {**summary, "settled_bets": settled, "retrained": retrained},
                   "errors": errors[-5:], "has_key": bool(key)})
        _save(STATUS_FILE, st)
        return st
    finally:
        _sync_lock.release()


@app.post("/api/sync")
async def sync_now():
    st = await asyncio.to_thread(run_sync, True)
    return {"status": "success", "data": st}


@app.get("/api/sync/status")
def sync_status():
    return {"status": "success", "data": _load(STATUS_FILE, {})}


async def _sync_loop():
    await asyncio.sleep(3)
    while True:
        try:
            if get_settings()["auto_sync"]:
                await asyncio.to_thread(run_sync, False)
        except Exception as e:  # 背景排程不可中斷
            print("同步失敗：", e)
        await asyncio.sleep(30 * 60)  # 每 30 分鐘檢查一次（MLB 官方每次都更新；國際盤依設定間隔）


def _import_dropped_json():
    """啟動時自動匯入放在程式資料夾的 cpbl_history_*.json，匯入後移到 data/imported/。"""
    for f in sorted(BASE.glob("cpbl_history_*.json")):
        try:
            info = cpbl_feed.import_json(json.loads(f.read_text(encoding="utf-8")))
            model.train("CPBL")
            (DATA / "imported").mkdir(exist_ok=True)
            os.replace(f, DATA / "imported" / f.name)
            print(f"已匯入 {f.name}：{info}")
        except Exception as e:
            print(f"匯入 {f.name} 失敗：{e}")


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(_import_dropped_json)
    asyncio.create_task(_sync_loop())
    if access._load().get("remote_enabled") and access.pin_set():
        asyncio.create_task(asyncio.to_thread(access.tunnel.start, PORT))


@app.on_event("shutdown")
async def _shutdown():
    access.tunnel.stop()


if __name__ == "__main__":
    print("=" * 60)
    print(f"BallMetrics 已啟動　電腦請開 http://127.0.0.1:{PORT}")
    for ip in access.lan_ips():
        print(f"手機（同一個 Wi-Fi）請開  http://{ip}:{PORT}")
    print("外出使用：到「參數設定 → 手機使用」設定 PIN 並開啟遠端連線")
    print("第一次啟動若出現 Windows 防火牆詢問，請勾選「私人網路」並按允許")
    print("=" * 60)
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
