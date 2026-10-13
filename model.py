# -*- coding: utf-8 -*-
"""
BallMetrics 勝率模型：以歷史戰績訓練的 Elo 評分系統

原理
  每隊有一個實力分數（起始 1500）。每場比賽依「賽前預測勝率」與「實際結果」的落差調整分數，
  大比分勝利調整較多；換季時分數向平均回歸（休賽期陣容變動）。
  主隊勝率 = 1 / (1 + 10^(-(主隊分數 + 主場優勢 − 客隊分數) / 400))

訓練
  K 值（學習速度）、主場優勢、換季回歸比例三個參數以網格搜尋挑選，
  用「最後一季」當驗證集（模型訓練時看不到），回報 Brier 分數、對數損失、命中率，
  並與「永遠猜主場勝率」的基準比較，確認模型真的有學到東西。

資料
  data/history/<聯盟>.csv  欄位：date,season,away,home,away_score,home_score,source
  MLB 可從 MLB 官方 statsapi 自動抓取；其他聯盟用 CSV 匯入，或在系統內填賽果自動累積。
"""
from __future__ import annotations

import csv
import io
import json
import math
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
HIST_DIR = BASE / "data" / "history"
MODEL_DIR = BASE / "data" / "models"
HIST_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = ["date", "season", "away", "home", "away_score", "home_score", "source",
          "game_pk", "away_sp_id", "home_sp_id", "away_sp", "home_sp"]


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# 隊名對照（英文 → 系統使用的中文隊名）
# ---------------------------------------------------------------------------
MLB_ZH = {
    "Arizona Diamondbacks": "亞利桑那響尾蛇", "Atlanta Braves": "亞特蘭大勇士", "Baltimore Orioles": "巴爾的摩金鶯",
    "Boston Red Sox": "波士頓紅襪", "Chicago Cubs": "芝加哥小熊", "Chicago White Sox": "芝加哥白襪",
    "Cincinnati Reds": "辛辛那提紅人", "Cleveland Guardians": "克利夫蘭守護者", "Cleveland Indians": "克利夫蘭守護者",
    "Colorado Rockies": "科羅拉多落磯", "Detroit Tigers": "底特律老虎", "Houston Astros": "休士頓太空人",
    "Kansas City Royals": "堪薩斯市皇家", "Los Angeles Angels": "洛杉磯天使", "Los Angeles Dodgers": "洛杉磯道奇",
    "Miami Marlins": "邁阿密馬林魚", "Milwaukee Brewers": "密爾瓦基釀酒人", "Minnesota Twins": "明尼蘇達雙城",
    "New York Mets": "紐約大都會", "New York Yankees": "紐約洋基", "Oakland Athletics": "運動家", "Athletics": "運動家",
    "Sacramento Athletics": "運動家", "Philadelphia Phillies": "費城費城人", "Pittsburgh Pirates": "匹茲堡海盜",
    "San Diego Padres": "聖地牙哥教士", "San Francisco Giants": "舊金山巨人", "Seattle Mariners": "西雅圖水手",
    "St. Louis Cardinals": "聖路易紅雀", "St Louis Cardinals": "聖路易紅雀", "Tampa Bay Rays": "坦帕灣光芒",
    "Texas Rangers": "德州遊騎兵", "Toronto Blue Jays": "多倫多藍鳥", "Washington Nationals": "華盛頓國民",
}
NBA_ZH = {
    "Atlanta Hawks": "亞特蘭大老鷹", "Boston Celtics": "波士頓塞爾提克", "Brooklyn Nets": "布魯克林籃網",
    "Charlotte Hornets": "夏洛特黃蜂", "Chicago Bulls": "芝加哥公牛", "Cleveland Cavaliers": "克里夫蘭騎士",
    "Dallas Mavericks": "達拉斯獨行俠", "Denver Nuggets": "丹佛金塊", "Detroit Pistons": "底特律活塞",
    "Golden State Warriors": "金州勇士", "Houston Rockets": "休士頓火箭", "Indiana Pacers": "印第安納溜馬",
    "Los Angeles Clippers": "洛杉磯快艇", "LA Clippers": "洛杉磯快艇", "Los Angeles Lakers": "洛杉磯湖人",
    "Memphis Grizzlies": "曼菲斯灰熊", "Miami Heat": "邁阿密熱火", "Milwaukee Bucks": "密爾瓦基公鹿",
    "Minnesota Timberwolves": "明尼蘇達灰狼", "New Orleans Pelicans": "紐奧良鵜鶘", "New York Knicks": "紐約尼克",
    "Oklahoma City Thunder": "奧克拉荷馬雷霆", "Orlando Magic": "奧蘭多魔術", "Philadelphia 76ers": "費城76人",
    "Phoenix Suns": "鳳凰城太陽", "Portland Trail Blazers": "波特蘭拓荒者", "Sacramento Kings": "沙加緬度國王",
    "San Antonio Spurs": "聖安東尼奧馬刺", "Toronto Raptors": "多倫多暴龍", "Utah Jazz": "猶他爵士",
    "Washington Wizards": "華盛頓巫師",
}
ALIASES = {**MLB_ZH, **NBA_ZH}


def season_of(date: str, league: str) -> str:
    """籃球賽季跨年（10 月開打），以「2026-27」表示；棒球以年份表示。"""
    y, m = int(date[:4]), int(date[5:7])
    if LEAGUE_SPORT.get(league.upper()) == "basketball":
        return f"{y}-{str(y + 1)[2:]}" if m >= 8 else f"{y - 1}-{str(y)[2:]}"
    return str(y)


def zh(name: str) -> str:
    return ALIASES.get((name or "").strip(), (name or "").strip())


# ---------------------------------------------------------------------------
# 歷史資料存取
# ---------------------------------------------------------------------------
def hist_path(league: str) -> Path:
    return HIST_DIR / f"{league.upper()}.csv"


def load_history(league: str) -> list[dict]:
    p = hist_path(league)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            try:
                rows.append({"date": r["date"][:10], "season": str(r.get("season") or r["date"][:4]),
                             "away": r["away"].strip(), "home": r["home"].strip(),
                             "away_score": float(r["away_score"]), "home_score": float(r["home_score"]),
                             "source": r.get("source", ""), "game_pk": _int(r.get("game_pk")),
                             "away_sp_id": _int(r.get("away_sp_id")), "home_sp_id": _int(r.get("home_sp_id")),
                             "away_sp": r.get("away_sp", ""), "home_sp": r.get("home_sp", "")})
            except (KeyError, ValueError, TypeError):
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def _key(r: dict) -> tuple:
    if r.get("game_pk"):
        return ("pk", int(r["game_pk"]))
    return (r["date"], r["away"], r["home"], int(r["away_score"]), int(r["home_score"]))


def save_history(league: str, rows: list[dict], replace: bool = False) -> int:
    """合併寫入（去除重複），回傳新增筆數。replace=True 時整份覆蓋。"""
    old = [] if replace else load_history(league)

    def ckey(r):
        return (r["date"], r["away"], r["home"], int(r["away_score"]), int(r["home_score"]))
    seen = {_key(r) for r in old} | {ckey(r) for r in old}
    added = 0
    for r in rows:
        k, ck = _key(r), ckey(r)
        if k not in seen and ck not in seen:
            seen.add(k)
            seen.add(ck)
            old.append(r)
            added += 1
    old.sort(key=lambda r: r["date"])
    with hist_path(league).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in old:
            w.writerow({k: (int(r[k]) if k.endswith("_score") else ("" if r.get(k) is None else r.get(k)))
                        for k in FIELDS})
    return added


def parse_csv_text(text: str, league: str = "") -> list[dict]:
    """匯入 CSV：需含 date,away,home,away_score,home_score（season 可省略）。也接受中文欄名。"""
    alias = {"日期": "date", "球季": "season", "客隊": "away", "主隊": "home", "客隊得分": "away_score",
             "主隊得分": "home_score", "客隊分數": "away_score", "主隊分數": "home_score"}
    reader = csv.DictReader(io.StringIO(text.strip().lstrip("﻿")))
    out = []
    for r in reader:
        r = {alias.get(k.strip(), k.strip()): (v or "").strip() for k, v in r.items() if k}
        try:
            d = r["date"].replace("/", "-")
            d = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
            out.append({"date": d, "season": r.get("season") or season_of(d, league), "away": r["away"], "home": r["home"],
                        "away_score": float(r["away_score"]), "home_score": float(r["home_score"]), "source": "csv"})
        except (KeyError, ValueError):
            continue
    return out


def fetch_mlb_history(seasons: list[int], timeout: int = 90) -> list[dict]:
    """從 MLB 官方 statsapi 抓取例行賽 + 季後賽完賽結果（含預計先發投手）。"""
    rows = []
    for yr in seasons:
        url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&season={yr}"
               f"&gameType=R,F,D,L,W&startDate={yr}-03-01&endDate={yr}-11-30&hydrate=probablePitcher")
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                a, h = g["teams"]["away"], g["teams"]["home"]
                if a.get("score") is None or h.get("score") is None or a["score"] == h["score"]:
                    continue
                ap, hp = a.get("probablePitcher") or {}, h.get("probablePitcher") or {}
                rows.append({"date": g.get("officialDate") or d["date"], "season": str(yr),
                             "away": zh(a["team"]["name"]), "home": zh(h["team"]["name"]),
                             "away_score": float(a["score"]), "home_score": float(h["score"]), "source": "mlb_statsapi",
                             "game_pk": g.get("gamePk"), "away_sp_id": ap.get("id"), "home_sp_id": hp.get("id"),
                             "away_sp": ap.get("fullName", ""), "home_sp": hp.get("fullName", "")})
    return rows


def fetch_mlb_full(seasons: list[int], progress=None) -> dict:
    """抓取賽果 + 所有先發投手逐場紀錄，並以『實際先發』校正預計先發。"""
    import pitching
    rows = fetch_mlb_history(seasons)
    pairs, names = set(), {}
    for r in rows:
        for side in ("away", "home"):
            pid = r.get(f"{side}_sp_id")
            if pid:
                pairs.add((int(pid), int(r["season"])))
                names[int(pid)] = r.get(f"{side}_sp", "")
    logs = pitching.fetch_logs(pairs, names)
    act = pitching.actual_starters(logs)
    fixed = 0
    for r in rows:
        a = act.get(r.get("game_pk"))
        if not a:
            continue
        for side in ("away", "home"):
            if a.get(side) and a[side] != r.get(f"{side}_sp_id"):
                r[f"{side}_sp_id"] = a[side]
                fixed += 1
    save_history("MLB", rows, replace=True)
    pitching.save_logs(logs, replace=True)
    return {"games": len(rows), "pitchers": len({p for p, _ in pairs}), "logs": len(logs), "sp_corrected": fixed}


# ---------------------------------------------------------------------------
# Elo 核心
# ---------------------------------------------------------------------------
def p_home(r_home: float, r_away: float, hfa: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(r_home + hfa - r_away) / 400.0))


def mov_mult(margin: float, elo_diff_winner: float) -> float:
    """大比分勝利調整較多，但對原本就被看好的強隊打折（避免分數膨脹）。"""
    return math.log(abs(margin) + 1) * (2.2 / (elo_diff_winner * 0.001 + 2.2))


def run_elo(rows: list[dict], k: float, hfa: float, regress: float, use_mov: bool = True,
            collect_from: str | None = None, adj: list[float] | None = None):
    """依時間順序跑 Elo，回傳 (最終分數, 預測紀錄[(p_home, y)], 每隊比賽數)。"""
    R: dict[str, float] = {}
    n_games: dict[str, int] = {}
    preds = []
    season = None
    for idx, r in enumerate(rows):
        if r["season"] != season:
            if season is not None and R:
                mean = sum(R.values()) / len(R)
                for t in R:
                    R[t] = mean + (R[t] - mean) * (1 - regress)
            season = r["season"]
        ra = R.setdefault(r["away"], 1500.0)
        rh = R.setdefault(r["home"], 1500.0)
        extra = adj[idx] if adj else 0.0  # 先發投手等賽前調整（Elo 分數）
        ph = p_home(rh, ra, hfa + extra)
        margin = r["home_score"] - r["away_score"]
        if margin == 0:
            y = 0.5
        else:
            y = 1.0 if margin > 0 else 0.0
        if collect_from is None or r["season"] >= collect_from:
            preds.append((ph, y, r))
        mult = 1.0
        if use_mov and margin != 0:
            diff_w = (rh + hfa + extra - ra) if margin > 0 else (ra - rh - hfa - extra)
            mult = mov_mult(margin, diff_w)
        delta = k * mult * (y - ph)
        R[r["home"]] = rh + delta
        R[r["away"]] = ra - delta
        n_games[r["home"]] = n_games.get(r["home"], 0) + 1
        n_games[r["away"]] = n_games.get(r["away"], 0) + 1
    return R, preds, n_games


def _metrics(preds) -> dict:
    if not preds:
        return {"n": 0}
    n = len(preds)
    brier = sum((p - y) ** 2 for p, y, *_ in preds) / n
    ll = -sum(math.log(max(min(p if y >= 0.5 else 1 - p, 1 - 1e-9), 1e-9)) for p, y, *_ in preds) / n
    acc = sum(1 for p, y, *_ in preds if (p >= 0.5) == (y >= 0.5)) / n
    return {"n": n, "brier": round(brier, 4), "logloss": round(ll, 4), "accuracy": round(acc * 100, 1)}


SPORT_GRID = {
    "baseball": {"k": [0.5, 1, 2, 3, 4, 5, 6, 8, 10, 14], "hfa": [0, 10, 20, 30, 40, 55], "regress": [0.25, 0.4, 0.55, 0.7]},
    "basketball": {"k": [8, 12, 16, 20, 25, 32], "hfa": [30, 50, 70, 90, 110], "regress": [0.2, 0.3, 0.45, 0.6]},
    "other": {"k": [10, 15, 20, 30, 40], "hfa": [0, 30, 60, 90], "regress": [0.2, 0.33, 0.5]},
}
LEAGUE_SPORT = {"MLB": "baseball", "CPBL": "baseball", "NPB": "baseball", "KBO": "baseball",
                "NBA": "basketball", "TPBL": "basketball", "PLG": "basketball", "BCL": "basketball"}


SP_LEAGUES = ("MLB", "CPBL")
SP_GRID = {"hl": [8, 15, 30, 60], "prior": [25, 50, 100], "offset": [0.5, 1.0, 1.5, 2.0], "c": [35, 50, 65]}


def _calibration(test_preds) -> list[dict]:
    buckets = {}
    for p, y, *_ in test_preds:
        fav = max(p, 1 - p)
        hit = y if p >= 0.5 else 1 - y
        b = min(int((fav - 0.5) * 20), 9)  # 每 5% 一組：50-55、55-60…
        d = buckets.setdefault(b, [0, 0.0, 0.0])
        d[0] += 1; d[1] += fav; d[2] += hit
    return [{"range": f"{50 + b * 5}-{55 + b * 5}%", "n": d[0],
             "predicted": round(d[1] / d[0] * 100, 1), "actual": round(d[2] / d[0] * 100, 1)}
            for b, d in sorted(buckets.items())]


def train(league: str, sport: str | None = None, search: bool = True) -> dict:
    """search=False：沿用上次挑好的參數，只用最新資料重算分數（快速，供自動更新用）。"""
    league = league.upper()
    rows = load_history(league)
    sport = sport or LEAGUE_SPORT.get(league, "other")
    if len(rows) < 30:
        raise ValueError(f"{league} 歷史資料只有 {len(rows)} 場，至少需要 30 場才能訓練")
    prev = load_model(league) if not search else None

    seasons = sorted({r["season"] for r in rows})
    # 驗證集：最後一季（場數夠）或最後 20% 比賽
    last = seasons[-1]
    n_last = sum(1 for r in rows if r["season"] == last)
    if len(seasons) >= 2 and n_last >= 60:
        split_desc = f"{last} 球季（{n_last} 場）"
        train_rows = [r for r in rows if r["season"] < last]
        test_from_idx = len(train_rows)
    else:
        test_from_idx = int(len(rows) * 0.8)
        train_rows = rows[:test_from_idx]
        split_desc = f"最後 {len(rows) - test_from_idx} 場"
    burn = seasons[1] if len(seasons) >= 3 else None  # 第一季當暖身，不計分

    # ---- 1) 球隊 Elo 參數 ----
    if prev:
        K, HFA, RG = prev["params"]["k"], prev["params"]["hfa"], prev["params"]["regress"]
    else:
        grid = SPORT_GRID.get(sport, SPORT_GRID["other"])
        best = None
        for k in grid["k"]:
            for hfa in grid["hfa"]:
                for rg in grid["regress"]:
                    _, preds, _ = run_elo(train_rows, k, hfa, rg, collect_from=burn)
                    if not preds:
                        continue
                    ll = _metrics(preds)["logloss"]
                    if best is None or ll < best[0]:
                        best = (ll, k, hfa, rg)
        _, K, HFA, RG = best

    _, base_all, _ = run_elo(rows, K, HFA, RG)
    m_base_elo = _metrics(base_all[test_from_idx:])

    # ---- 2) 先發投手近況（MLB）----
    sp_block, adj = None, None
    if league in SP_LEAGUES:
        import pitching
        logs = pitching.load_logs(league)
        cover = sum(1 for r in rows if r.get("home_sp_id") and r.get("away_sp_id")) / len(rows)
        # 投手紀錄完整度：先發投手在該季有逐場紀錄的比例（資料不完整時不啟用，避免把缺資料當成爛投手）
        have = {(r["pid"], r["season"]) for r in logs if r["gs"] == 1}
        season_cov = {}
        for ssn in seasons:
            need = [(r.get(f"{sd}_sp_id"), ssn) for r in rows if r["season"] == ssn for sd in ("away", "home")
                    if r.get(f"{sd}_sp_id")]
            season_cov[ssn] = sum(1 for k in need if k in have) / len(need) if need else 0
        covered = {ssn for ssn, cv in season_cov.items() if cv >= 0.85}
        log_cover = sum(season_cov.values()) / len(season_cov) if season_cov else 0
        train_cov = covered & {r["season"] for r in train_rows}
        # 需要：驗證季資料完整，且訓練期至少有一季完整（才能挑參數）
        if logs and cover > 0.5 and last in covered and train_cov:
            lc = pitching.league_consts(logs, {r["season"] for r in train_rows})
            ntr = len(train_rows)
            if prev and prev.get("sp"):
                sp_params = prev["sp"]["params"]
            else:
                _, bp, _ = run_elo(train_rows, K, HFA, RG, collect_from=burn)
                base_ll = _metrics(bp)["logloss"]
                best = None
                for hl in SP_GRID["hl"]:
                    for pr in SP_GRID["prior"]:
                        for off in SP_GRID["offset"]:
                            vals, _ = pitching.game_values(rows, logs, hl, pr, off, lc["lg_raw"], covered)
                            for c in SP_GRID["c"]:
                                a = [c * (h - w) for h, w in vals[:ntr]]
                                _, p, _ = run_elo(train_rows, K, HFA, RG, collect_from=burn, adj=a)
                                ll = _metrics(p)["logloss"]
                                if best is None or ll < best[0]:
                                    best = (ll, hl, pr, off, c)
                sp_params = None
                if best and best[0] < base_ll:
                    sp_params = {"hl": best[1], "prior": best[2], "offset": best[3], "c": best[4]}
            if sp_params:
                vals, tracker = pitching.game_values(rows, logs, sp_params["hl"], sp_params["prior"],
                                                     sp_params["offset"], lc["lg_raw"], covered)
                adj = [sp_params["c"] * (h - w) for h, w in vals]
                recent_seasons = set(seasons[-2:])
                active = {r["pid"] for r in logs if r["season"] in recent_seasons}
                names = {}
                for r in logs:
                    names[r["pid"]] = r["name"]
                sp_block = {
                    "params": sp_params, "lg_raw": lc["lg_raw"], "cfip": lc["cfip"], "lg_era": lc["lg_era"],
                    "states": {str(pid): [round(x, 4) for x in st] for pid, st in tracker.s.items() if pid in active},
                    "names": {str(pid): names.get(pid, "") for pid in active},
                    "name_index": {pitching.norm_name(names.get(pid, "")): pid for pid in active if names.get(pid)},
                    "logs_through": max(r["date"] for r in logs) if logs else None,
                    "log_coverage": round(log_cover * 100, 1),
                    "covered_seasons": sorted(covered),
                }

    # ---- 3) 驗證（模型參數沒看過驗證區段）----
    _, all_preds, _ = run_elo(rows, K, HFA, RG, adj=adj)
    test_preds = all_preds[test_from_idx:]
    m_test = _metrics(test_preds)
    home_rate = sum(1 for r in train_rows if r["home_score"] > r["away_score"]) / max(len(train_rows), 1)
    m_base = _metrics([(home_rate, y) for _, y, *_ in test_preds])

    # ---- 4) 最終模型 ----
    R, _, n_games = run_elo(rows, K, HFA, RG, adj=adj)
    sp_note = None
    if league in SP_LEAGUES and not sp_block:
        try:
            done = "、".join(sorted(covered)) or "無"
            sp_note = (f"投手資料完整的球季：{done}（需驗證季 {last} 與至少一個訓練季完整，才啟用先發投手調整）")
        except NameError:
            sp_note = "尚無投手逐場紀錄"
    out = {
        "league": league, "sport": sport, "sp_note": sp_note,
        "params": {"k": K, "hfa": HFA, "regress": RG},
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "games": len(rows), "seasons": seasons, "last_game": rows[-1]["date"],
        "last_season": last,
        "validation": {"split": split_desc, "model": m_test, "baseline": m_base,
                       "baseline_desc": f"永遠預測主隊勝率 {home_rate * 100:.1f}%",
                       "beats_baseline": m_test.get("brier", 1) < m_base.get("brier", 1),
                       "elo_only": m_base_elo if sp_block else None,
                       "calibration": _calibration(test_preds)},
        "ratings": {t: round(v, 1) for t, v in sorted(R.items(), key=lambda x: -x[1])},
        "team_games": n_games,
        "sp": sp_block,
    }
    (MODEL_DIR / f"{league}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def sp_value(m: dict, pid=None, name: str = "") -> dict | None:
    """以模型中的投手近況計算單一先發的影響。"""
    sp = (m or {}).get("sp")
    if not sp:
        return None
    import pitching
    if not pid and name:
        pid = sp["name_index"].get(pitching.norm_name(name))
    tr = pitching.FormTracker(sp["params"]["hl"], sp["params"]["prior"], sp["params"]["offset"], sp["lg_raw"])
    st = sp["states"].get(str(pid)) if pid else None
    if st:
        tr.s[int(pid)] = st
    raw, ipx, rs = tr.value(int(pid) if pid else None)
    return {"pid": int(pid) if pid else None, "name": sp["names"].get(str(pid), name) if pid else name,
            "known": bool(st), "fip": round(raw + sp["cfip"], 2), "ip_exp": round(ipx, 1),
            "runs_saved": round(rs, 2), "elo": round(sp["params"]["c"] * rs, 1),
            "weighted_starts": round(st[2], 1) if st else 0}


def load_model(league: str) -> dict | None:
    p = MODEL_DIR / f"{league.upper()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_models() -> list[dict]:
    out = []
    leagues = {p.stem for p in HIST_DIR.glob("*.csv") if "_" not in p.stem} | {p.stem for p in MODEL_DIR.glob("*.json")}
    for lg in sorted(leagues):
        m = load_model(lg)
        out.append({
            "league": lg, "history_games": len(load_history(lg)),
            "trained": bool(m), "trained_at": m and m["trained_at"], "params": m and m["params"],
            "validation": m and m["validation"], "last_game": m and m["last_game"],
            "sp_note": m and m.get("sp_note"),
            "sp": m and m.get("sp") and {"params": m["sp"]["params"], "pitchers": len(m["sp"]["states"]),
                                         "covered_seasons": m["sp"].get("covered_seasons"),
                                         "logs_through": m["sp"].get("logs_through"),
                                         "lg_era": round(m["sp"]["lg_era"], 2)},
            "ratings": m and [{"team": t, "rating": r, "games": m["team_games"].get(t, 0)}
                              for t, r in list(m["ratings"].items())],
        })
    return out


def predict(league: str, away: str, home: str, model: dict | None = None,
            away_sp: str = "", home_sp: str = "", away_sp_id=None, home_sp_id=None) -> dict | None:
    """回傳 Elo 預測（MLB 含先發投手調整）；任一隊不在模型中則回傳 None。"""
    m = model or load_model(league)
    if not m:
        return None
    R = m["ratings"]
    a, h = zh(away), zh(home)
    if a not in R or h not in R:
        return None
    sp_a = sp_h = None
    extra = 0.0
    def announced(pid, name):
        return bool(pid) or (bool((name or "").strip()) and name.strip() not in ("官方未公布", "TBD", "未定"))
    if m.get("sp") and announced(away_sp_id, away_sp) and announced(home_sp_id, home_sp):
        # 兩隊先發都公布才調整；未公布時只用球隊實力
        sp_a = sp_value(m, away_sp_id, away_sp)
        sp_h = sp_value(m, home_sp_id, home_sp)
        # 名字比對不到（也沒有官方 ID）→ 可能是先發填錯或打錯字，不做調整並提醒
        unmatched = [v["name"] for v in (sp_a, sp_h) if v and not v["pid"]]
        if unmatched:
            for v in (sp_a, sp_h):
                if v and not v["pid"]:
                    v["unmatched"] = True
        elif sp_a and sp_h:
            extra = sp_h["elo"] - sp_a["elo"]
    ph = p_home(R[h], R[a], m["params"]["hfa"] + extra)
    ph0 = p_home(R[h], R[a], m["params"]["hfa"])
    return {"p_home": ph, "p_away": 1 - ph, "r_home": R[h], "r_away": R[a],
            "hfa": m["params"]["hfa"], "trained_at": m["trained_at"],
            "p_home_team_only": ph0, "sp_away": sp_a, "sp_home": sp_h, "sp_elo_adj": round(extra, 1)}
