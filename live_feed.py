# -*- coding: utf-8 -*-
"""
即時資料同步
1. MLB 官方 statsapi（免費、免金鑰）：自動建立賽程、更新預計先發投手、場地、系列賽階段，賽後自動填入比分
2. The Odds API（需免費申請金鑰 https://the-odds-api.com）：國際博彩公司即時賠率
   → 計算多家公司的「去水共識機率」作為市場參考，記錄盤口走勢
   ※ 台灣運彩官網有防爬蟲保護，運彩賠率仍需手動輸入
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import model

TW = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 BallMetrics/2.0"}

SPORT_LEAGUE = {  # The Odds API sport_key → 系統聯盟代碼
    "baseball_mlb": ("MLB", "baseball"),
    "baseball_npb": ("NPB", "baseball"),
    "baseball_kbo": ("KBO", "baseball"),
    "basketball_nba": ("NBA", "basketball"),
    "basketball_wnba": ("WNBA", "basketball"),
    "basketball_euroleague": ("EUROLEAGUE", "basketball"),
}


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), dict(r.headers)


def _utc_to_tw(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TW)


def _kickoff(g: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{g['date']} {g['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TW)
    except Exception:
        return None


def find_game(games: list[dict], league: str, away: str, home: str, ko: datetime, hours: float = 18):
    """以聯盟 + 隊名 + 開賽時間（±hours）找出同一場比賽，多場時取時間最接近者。"""
    best, best_diff = None, None
    for g in games:
        if g.get("league", "").upper() != league:
            continue
        if model.zh(g.get("away_team", "")) != away or model.zh(g.get("home_team", "")) != home:
            continue
        k = _kickoff(g)
        if not k:
            continue
        diff = abs((k - ko).total_seconds()) / 3600
        if diff <= hours and (best_diff is None or diff < best_diff):
            best, best_diff = g, diff
    return best


def _new_game(gid, league, sport, ko: datetime, away, home, **kw) -> dict:
    g = {
        "id": gid, "league": league, "sport": sport, "stage": kw.get("stage", ""),
        "date": ko.strftime("%Y-%m-%d"), "time": ko.strftime("%H:%M"), "venue": kw.get("venue", ""),
        "away_team": away, "away_starter": kw.get("away_starter", ""),
        "home_team": home, "home_starter": kw.get("home_starter", ""),
        "away_odds": None, "home_odds": None, "draw_odds": None,
        "model_p_away": None, "model_p_home": None, "model_p_draw": None,
        "open_away_odds": None, "open_home_odds": None,
        "model_source": "auto", "result": None, "score": "", "source": kw.get("source", ""),
        "report": {"why_bet": "", "pitcher_eval": "", "batting_eval": "", "defense_eval": "",
                   "team_winrate": "", "home_away": ""},
    }
    return g


# ---------------------------------------------------------------------------
# MLB 官方
# ---------------------------------------------------------------------------
def sync_mlb(games: list[dict], days_back: int = 3, days_ahead: int = 3, auto_create: bool = True) -> dict:
    today = datetime.now(TW).date()
    start = (today - timedelta(days=days_back + 1)).isoformat()
    end = (today + timedelta(days=days_ahead)).isoformat()
    url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}&endDate={end}"
           f"&gameType=R,F,D,L,W&hydrate=probablePitcher")
    data, _ = _get_json(url)
    rep = {"created": 0, "updated": 0, "results": 0, "new_history": [], "new_logs": 0}
    now = datetime.now(TW)
    refresh: set[tuple[int, int]] = set()   # 需要更新逐場紀錄的投手（近期先發 + 剛完賽）
    must: set[int] = set()                  # 剛完賽的先發，一定要更新
    for d in data.get("dates", []):
        for sg in d.get("games", []):
            st = sg.get("status", {})
            if st.get("detailedState") in ("Postponed", "Cancelled", "Suspended"):
                continue
            a, h = sg["teams"]["away"], sg["teams"]["home"]
            away, home = model.zh(a["team"]["name"]), model.zh(h["team"]["name"])
            ko = _utc_to_tw(sg["gameDate"])
            g = find_game(games, "MLB", away, home, ko)
            final = st.get("abstractGameState") == "Final" and a.get("score") is not None
            stage = ""
            if sg.get("gameType") != "R":
                stage = f"{sg.get('seriesDescription', '季後賽')} G{sg.get('seriesGameNumber', '')}"
                stage = (stage.replace("AL Championship Series", "美聯冠軍賽")
                         .replace("NL Championship Series", "國聯冠軍賽").replace("World Series", "世界大賽")
                         .replace("AL Division Series", "美聯分區賽").replace("NL Division Series", "國聯分區賽")
                         .replace("AL Wild Card Series", "美聯外卡賽").replace("NL Wild Card Series", "國聯外卡賽"))
            asp = (a.get("probablePitcher") or {}).get("fullName", "")
            hsp = (h.get("probablePitcher") or {}).get("fullName", "")
            asp_id = (a.get("probablePitcher") or {}).get("id")
            hsp_id = (h.get("probablePitcher") or {}).get("id")
            for pid in (asp_id, hsp_id):
                if pid:
                    refresh.add((int(pid), int(sg.get("season") or ko.year)))
                    if final:
                        must.add(int(pid))

            if g is None:
                if final or not auto_create or ko < now:
                    if final:  # 沒有對應賽事也要納入歷史資料
                        rep["new_history"].append(_hist_row(sg, away, home))
                    continue
                g = _new_game(f"MLB_{sg['gamePk']}", "MLB", "baseball", ko, away, home, stage=stage or "例行賽",
                              venue=sg.get("venue", {}).get("name", ""), away_starter=asp or "官方未公布",
                              home_starter=hsp or "官方未公布", source="mlb_statsapi")
                g.update({"game_pk": sg["gamePk"], "away_sp_id": asp_id, "home_sp_id": hsp_id,
                          "away_sp_src": asp or "官方未公布", "home_sp_src": hsp or "官方未公布"})
                games.append(g)
                rep["created"] += 1
                continue

            changed = False
            # 官方公布的預計先發優先（以官方為準）
            g["game_pk"] = sg["gamePk"]
            for side, sp, sp_id in (("away", asp, asp_id), ("home", hsp, hsp_id)):
                sp = sp or ("" if final else "官方未公布")
                if sp and g.get(f"{side}_starter") != sp:
                    g[f"{side}_starter"] = sp
                    changed = True
                if sp:
                    g[f"{side}_sp_id"], g[f"{side}_sp_src"] = sp_id, sp
            # 開賽時間以官方為準
            if g.get("date") != ko.strftime("%Y-%m-%d") or g.get("time") != ko.strftime("%H:%M"):
                if not final:
                    g["date"], g["time"] = ko.strftime("%Y-%m-%d"), ko.strftime("%H:%M")
                    changed = True
            if final and not g.get("result"):
                g["result"] = "home" if h["score"] > a["score"] else "away"
                g["score"] = f"{int(a['score'])}-{int(h['score'])}"
                rep["results"] += 1
                rep["new_history"].append(_hist_row(sg, away, home))
            elif changed:
                rep["updated"] += 1

    # 更新先發投手逐場紀錄（每位投手 12 小時內只抓一次；剛完賽的一定抓）
    try:
        import pitching
        cache_f = pitching.pitch_file("MLB").parent / "pitch_fetch.json"
        cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
        now_ts = now.timestamp()
        todo = {(pid, yr) for pid, yr in refresh
                if pid in must or now_ts - cache.get(str(pid), 0) > 12 * 3600}
        if todo:
            logs = pitching.fetch_logs(todo, workers=6)
            rep["new_logs"] = pitching.save_logs(logs)
            for pid, _ in todo:
                cache[str(pid)] = now_ts
            cache_f.write_text(json.dumps(cache), encoding="utf-8")
            act = pitching.actual_starters(logs)
            for r in rep["new_history"]:  # 以實際先發校正
                a2 = act.get(r.get("game_pk")) or {}
                for side in ("away", "home"):
                    if a2.get(side):
                        r[f"{side}_sp_id"] = a2[side]
    except Exception as e:
        rep["pitch_error"] = str(e)
    return rep


def _hist_row(sg, away, home):
    a, h = sg["teams"]["away"], sg["teams"]["home"]
    ap, hp = a.get("probablePitcher") or {}, h.get("probablePitcher") or {}
    return {"date": sg.get("officialDate"), "season": str(sg.get("season")), "away": away, "home": home,
            "away_score": float(a["score"]), "home_score": float(h["score"]), "source": "mlb_statsapi",
            "game_pk": sg.get("gamePk"), "away_sp_id": ap.get("id"), "home_sp_id": hp.get("id"),
            "away_sp": ap.get("fullName", ""), "home_sp": hp.get("fullName", "")}


# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------
def consensus(event: dict) -> dict | None:
    """多家公司各自去水後取平均 = 共識機率；另回傳最佳賠率與 Pinnacle（公認最精準的盤口）。"""
    home, away = event["home_team"], event["away_team"]
    probs, best = [], {"home": 0.0, "away": 0.0, "draw": 0.0}
    pin = None
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            price = {}
            for o in mk.get("outcomes", []):
                side = "home" if o["name"] == home else ("away" if o["name"] == away else "draw")
                price[side] = float(o["price"])
            if "home" not in price or "away" not in price or min(price.values()) <= 1:
                continue
            inv = {s: 1 / p for s, p in price.items()}
            tot = sum(inv.values())
            nv = {s: v / tot for s, v in inv.items()}
            probs.append(nv)
            for s, p in price.items():
                best[s] = max(best[s], p)
            if bk.get("key") == "pinnacle":
                pin = {"home": round(nv["home"] * 100, 2), "away": round(nv["away"] * 100, 2),
                       "odds_home": price["home"], "odds_away": price["away"]}
    if not probs:
        return None
    n = len(probs)
    ph = sum(p["home"] for p in probs) / n
    pa = sum(p["away"] for p in probs) / n
    pd = sum(p.get("draw", 0) for p in probs) / n
    return {"p_home": round(ph * 100, 2), "p_away": round(pa * 100, 2), "p_draw": round(pd * 100, 2) if pd else None,
            "books": n, "best_home": best["home"] or None, "best_away": best["away"] or None,
            "pinnacle": pin}


def sync_odds_api(games: list[dict], api_key: str, sports: list[str], regions: str = "eu",
                  auto_create: bool = True, days_ahead: int = 2) -> dict:
    rep = {"created": 0, "updated": 0, "events": 0, "quota_remaining": None, "quota_used": None, "errors": []}
    # 先查哪些聯盟目前有賽季（/sports 不扣額度），避免浪費額度
    try:
        active, _ = _get_json(f"https://api.the-odds-api.com/v4/sports/?apiKey={urllib.parse.quote(api_key)}")
        active_keys = {s["key"] for s in active if s.get("active")}
    except Exception as e:
        rep["errors"].append(f"無法取得聯盟清單：{e}")
        return rep
    now = datetime.now(TW)
    horizon = now + timedelta(days=days_ahead)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for sk in sports:
        if sk not in active_keys:
            continue
        league, sport = SPORT_LEAGUE.get(sk, (sk.split("_")[-1].upper(), "other"))
        qs = urllib.parse.urlencode({"apiKey": api_key, "regions": regions, "markets": "h2h", "oddsFormat": "decimal"})
        try:
            events, hdr = _get_json(f"https://api.the-odds-api.com/v4/sports/{sk}/odds/?{qs}")
        except Exception as e:
            rep["errors"].append(f"{sk}：{e}")
            continue
        rep["quota_remaining"] = hdr.get("x-requests-remaining") or hdr.get("X-Requests-Remaining")
        rep["quota_used"] = hdr.get("x-requests-used") or hdr.get("X-Requests-Used")
        for ev in events:
            ko = _utc_to_tw(ev["commence_time"])
            if ko < now - timedelta(hours=4):
                continue
            c = consensus(ev)
            if not c:
                continue
            rep["events"] += 1
            away, home = model.zh(ev["away_team"]), model.zh(ev["home_team"])
            g = find_game(games, league, away, home, ko)
            if g is None:
                if not auto_create or ko > horizon:
                    continue
                g = _new_game(f"{league}_{ev['id'][:10]}", league, sport, ko, away, home, stage="例行賽",
                              source="odds_api")
                games.append(g)
                rep["created"] += 1
            else:
                rep["updated"] += 1
            g["market"] = {**c, "updated_at": stamp}
            hist = g.setdefault("market_history", [])
            if not hist or hist[-1]["p_home"] != c["p_home"] or hist[-1].get("best_home") != c["best_home"]:
                hist.append({"t": stamp, "p_home": c["p_home"], "best_home": c["best_home"], "best_away": c["best_away"]})
                del hist[:-60]
    return rep


def sync_scores_odds_api(games: list[dict], api_key: str, sports: list[str]) -> dict:
    """非 MLB 聯盟：用 The Odds API 的 scores（近 3 天）自動填賽果。每次每聯盟扣 2 額度。"""
    rep = {"results": 0, "new_history": {}, "errors": []}
    for sk in sports:
        if sk == "baseball_mlb":
            continue  # MLB 用官方資料
        league, _ = SPORT_LEAGUE.get(sk, (sk.split("_")[-1].upper(), "other"))
        qs = urllib.parse.urlencode({"apiKey": api_key, "daysFrom": 3})
        try:
            events, _ = _get_json(f"https://api.the-odds-api.com/v4/sports/{sk}/scores/?{qs}")
        except Exception as e:
            rep["errors"].append(f"{sk} 賽果：{e}")
            continue
        for ev in events:
            if not ev.get("completed") or not ev.get("scores"):
                continue
            sc = {s["name"]: float(s["score"]) for s in ev["scores"]}
            hs, as_ = sc.get(ev["home_team"]), sc.get(ev["away_team"])
            if hs is None or as_ is None:
                continue
            away, home = model.zh(ev["away_team"]), model.zh(ev["home_team"])
            ko = _utc_to_tw(ev["commence_time"])
            row = {"date": ko.strftime("%Y-%m-%d"), "season": model.season_of(ko.strftime("%Y-%m-%d"), league),
                   "away": away, "home": home,
                   "away_score": as_, "home_score": hs, "source": "odds_api"}
            rep["new_history"].setdefault(league, []).append(row)
            g = find_game(games, league, away, home, ko)
            if g and not g.get("result") and hs != as_:
                g["result"] = "home" if hs > as_ else "away"
                g["score"] = f"{int(as_)}-{int(hs)}"
                rep["results"] += 1
    return rep
