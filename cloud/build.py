# -*- coding: utf-8 -*-
"""
GitHub Actions 雲端更新程式（每 2 小時執行）

流程
  1. 下載並解密上次的狀態（歷史資料、模型、賽事），金鑰：STATE_KEY
  2. 同步：MLB 官方賽程/先發/賽果/投手紀錄、中職進階數據網站賽程/賽果/box score、
     The Odds API 國際盤（有設定 ODDS_API_KEY 才會抓）
  3. 有新資料就更新 Elo＋先發投手模型
  4. 產生網頁要用的資料，以你的網站密碼（BM_PASSWORD）加密 → site/data.enc.json
  5. 重新加密狀態，交給 workflow 存回 Release 附件

所有資料在儲存庫與網站上都是加密的；沒有密碼的人只看得到程式碼。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud"))

import crypto  # noqa: E402

DATA = ROOT / "data"
STATE_FILES = ("history", "models", "games.json", "feed_status.json")


# ---------------------------------------------------------------------------
# 狀態存取
# ---------------------------------------------------------------------------
def load_state(path: Path, key: str) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        print("找不到狀態檔，從空白開始")
        return False
    raw = crypto.decrypt(json.loads(path.read_text()), key)
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        tf.extractall(DATA, filter="data")
    print("已載入狀態：", sorted(p.name for p in DATA.iterdir()))
    return True


def save_state(path: Path, key: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in STATE_FILES:
            p = DATA / name
            if p.exists():
                tf.add(p, arcname=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(crypto.encrypt(buf.getvalue(), key)))
    print(f"狀態已加密存檔（{path.stat().st_size / 1024:.0f} KB）")


# ---------------------------------------------------------------------------
# 網頁資料
# ---------------------------------------------------------------------------
GAME_KEYS = ("id", "league", "sport", "stage", "date", "time", "venue", "away_team", "home_team",
             "away_starter", "home_starter", "away_sp_id", "home_sp_id", "away_sp_src", "home_sp_src",
             "away_odds", "home_odds", "draw_odds", "model_p_away", "model_p_home", "model_source",
             "result", "score", "report", "source", "market")


def model_summary(league: str) -> dict | None:
    import model
    import pitching
    m = model.load_model(league)
    if not m:
        return None
    out = {"league": league, "params": m["params"], "trained_at": m["trained_at"], "games": m["games"],
           "last_game": m["last_game"], "validation": m["validation"], "sp_note": m.get("sp_note"),
           "ratings": m["ratings"], "team_games": m.get("team_games", {}), "sp": None}
    sp = m.get("sp")
    if sp:
        pitchers = []
        for pid in sp["states"]:
            v = model.sp_value(m, int(pid))
            if not v:
                continue
            v["recent"] = pitching.recent_starts(int(pid), league=league)
            pitchers.append(v)
        unknown = model.sp_value(m, None, "?")
        out["sp"] = {"params": sp["params"], "lg_era": round(sp["lg_era"], 2), "cfip": sp["cfip"],
                     "covered_seasons": sp.get("covered_seasons"), "logs_through": sp.get("logs_through"),
                     "pitchers": pitchers, "unknown": unknown}
    return out


def build_payload(games: list[dict], status: dict) -> dict:
    import engine
    gs = []
    for g in games:
        x = {k: g.get(k) for k in GAME_KEYS if g.get(k) not in (None, "", {})}
        if g.get("market_history"):
            x["market_history"] = g["market_history"][-30:]
        x["status"] = engine.game_status(g)
        gs.append(x)
    gs.sort(key=lambda x: (x["status"] == "settled", x["date"], x["time"]))
    models = {lg: model_summary(lg) for lg in ("MLB", "CPBL")}
    return {"generated_at": datetime.now(engine.TW).strftime("%Y-%m-%d %H:%M"), "games": gs,
            "models": {k: v for k, v in models.items() if v}, "status": status}


# ---------------------------------------------------------------------------
def main():
    state_path = Path(os.environ.get("STATE_PATH", ROOT / "cloud_state" / "state.enc"))
    site_dir = Path(os.environ.get("SITE_DIR", ROOT / "site"))
    state_key = os.environ.get("STATE_KEY", "")
    password = os.environ.get("BM_PASSWORD", "")
    if not state_key:
        sys.exit("缺少 STATE_KEY（請在 GitHub Secrets 設定）")
    load_state(state_path, state_key)

    import cpbl_feed
    import engine
    import live_feed
    import model

    games_f = DATA / "games.json"
    games = json.loads(games_f.read_text(encoding="utf-8")) if games_f.exists() else []
    now = datetime.now(engine.TW)
    errors, summary = [], {"created": 0, "updated": 0, "results": 0}
    new_hist: dict[str, list] = {}
    new_logs: set[str] = set()

    # 1) MLB 官方
    try:
        r = live_feed.sync_mlb(games)
        for k in summary:
            summary[k] += r.get(k, 0)
        new_hist.setdefault("MLB", []).extend(r["new_history"])
        if r.get("new_logs"):
            new_logs.add("MLB")
        if r.get("pitch_error"):
            errors.append("MLB 投手紀錄：" + r["pitch_error"])
    except Exception as e:
        errors.append(f"MLB 官方資料：{e}")

    # 2) 中職進階數據網站（海外可連）
    try:
        r = cpbl_feed.sync_cpbl_stats(games)
        for k in summary:
            summary[k] += r.get(k, 0)
        new_hist.setdefault("CPBL", []).extend(r["new_history"])
        if r.get("new_logs"):
            new_logs.add("CPBL")
    except Exception as e:
        errors.append(f"中職進階數據：{e}")

    # 3) 國際盤（選用）
    odds_key = os.environ.get("ODDS_API_KEY", "")
    quota = None
    if odds_key:
        sports = [s.strip() for s in os.environ.get("ODDS_SPORTS", "baseball_mlb,basketball_nba").split(",") if s.strip()]
        r = live_feed.sync_odds_api(games, odds_key, sports, os.environ.get("ODDS_REGIONS", "eu"), True)
        summary["created"] += r["created"]
        summary["updated"] += r["updated"]
        errors += r["errors"]
        quota = r.get("quota_remaining")

    # 4) 歷史 & 模型
    retrained = []
    for lg in set(new_hist) | new_logs:
        added = model.save_history(lg, new_hist.get(lg, [])) if new_hist.get(lg) else 0
        if added or lg in new_logs:
            try:
                prev = model.load_model(lg)
                model.train(lg, search=prev is None or (lg in model.SP_LEAGUES and not prev.get("sp")))
                retrained.append(lg)
            except ValueError as e:
                errors.append(str(e))
    # 每週一凌晨重新搜尋一次最佳參數
    if now.weekday() == 0 and now.hour < 3:
        for lg in ("MLB", "CPBL"):
            try:
                model.train(lg, search=True)
                retrained.append(lg + "（完整）")
            except Exception as e:
                errors.append(f"{lg} 重新搜尋參數：{e}")

    # 5) 清理過舊賽事
    def keep(g):
        ko = live_feed._kickoff(g)
        if not ko:
            return True
        return ko > now - timedelta(days=10 if g.get("result") else 3)
    games = [g for g in games if keep(g)]
    games_f.write_text(json.dumps(games, ensure_ascii=False, indent=1), encoding="utf-8")

    status = {"last_run": now.strftime("%Y-%m-%d %H:%M"), "summary": {**summary, "retrained": retrained},
              "errors": errors[-6:], "odds_quota": quota, "has_odds_key": bool(odds_key)}
    (DATA / "feed_status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=1))

    # 6) 網站資料（以網站密碼加密）
    site_dir.mkdir(parents=True, exist_ok=True)
    if password:
        payload = build_payload(games, status)
        (site_dir / "data.enc.json").write_text(crypto.encrypt_json(payload, password))
        print(f"網站資料已加密：{len(payload['games'])} 場賽事")
    else:
        print("⚠ 尚未設定 BM_PASSWORD，網站資料不會更新")
    save_state(state_path, state_key)


if __name__ == "__main__":
    t = time.time()
    main()
    print(f"完成，耗時 {time.time() - t:.0f} 秒")
