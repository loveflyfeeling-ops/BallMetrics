# -*- coding: utf-8 -*-
"""
BallMetrics 計算引擎
所有 EV、凱利注碼、去水機率、統計數字一律由這裡計算，前端與資料檔不再手填。

名詞：
  implied p  = 1 / 賠率（含莊家抽水）
  overround  = 各選項 implied p 加總（>1 的部分就是抽水）
  fair p     = 去水後的市場機率
  EV         = 模型勝率 × 賠率 − 1
  edge       = 模型勝率 − 市場去水機率
  Kelly      = (p × odds − 1) / (odds − 1)
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))  # 台灣時間（Windows 不需安裝 tzdata）

SIDES = ("away", "home", "draw")
SIDE_LABEL = {"away": "客隊", "home": "主隊", "draw": "和局"}

DEFAULT_SETTINGS = {
    "bankroll": 100000,        # 起始資金（TWD）
    "kelly_fraction": 0.25,    # 分數凱利（0.25 = 1/4 凱利）
    "min_ev": 3.0,             # 進場 EV 門檻（%）
    "min_edge": 2.0,           # 模型勝率需高於市場去水機率幾個百分點
    "max_stake_pct": 3.0,      # 單注上限（佔資金 %）
    "min_odds": 1.30,
    "max_odds": 4.00,
    "devig_method": "power",   # proportional / power
    "stake_round": 10,         # 注金進位單位（元）
    "daily_loss_limit_pct": 10.0,  # 單日虧損上限（佔資金 %），超過即停止建議
    # ---- 自動勝率模型 ----
    "model_mode": "blend",     # blend = Elo 與國際盤共識混合；elo = 只用 Elo；market = 只用國際盤
    "elo_weight": 0.3,         # 混合時 Elo 所佔權重（其餘為國際盤共識）
    # ---- 即時資料同步 ----
    "auto_sync": True,
    "sync_interval_min": 180,  # The Odds API 免費額度有限，建議 ≥ 120 分鐘
    "odds_api_key": "",
    "odds_regions": "eu",      # eu 含 Pinnacle
    "odds_sports": ["baseball_mlb", "basketball_nba"],
    "mlb_auto_create": True,
    "odds_auto_create": True,
    "fetch_scores": False,     # 非 MLB 聯盟用 The Odds API 抓賽果（每聯盟每次扣 2 額度）
    "auto_retrain": True,
    "cpbl_sync": True,         # 中職官網同步（需台灣網路）
    "cpbl_auto_create": True,
}


# ---------------------------------------------------------------------------
# 基礎機率
# ---------------------------------------------------------------------------
def implied(odds: float) -> float:
    return 1.0 / odds if odds and odds > 1 else 0.0


def devig(odds_list: list[float], method: str = "power") -> list[float]:
    """把含水賠率轉成去水機率。
    proportional：等比例縮放（最常見）
    power：p_i = (1/o_i)^k，求 k 使加總=1；會把較多抽水分配給冷門，
           較貼近實際「冷門偏誤」(favourite-longshot bias)。"""
    imps = [implied(o) for o in odds_list]
    total = sum(imps)
    if total <= 0:
        return [0.0 for _ in imps]
    if method != "power" or total <= 1.0:
        return [i / total for i in imps]
    lo, hi = 1.0, 10.0
    for _ in range(100):
        k = (lo + hi) / 2
        s = sum(i ** k for i in imps)
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    ps = [i ** k for i in imps]
    s = sum(ps)
    return [p / s for p in ps]


def ev(p: float, odds: float) -> float:
    """期望報酬率（小數），p 為 0~1。"""
    return p * odds - 1.0


def kelly(p: float, odds: float) -> float:
    """全凱利比例（小數），負值代表不該下注。"""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    return (p * odds - 1.0) / b


def target_odds(p: float, min_ev_pct: float) -> float | None:
    """要達到 EV 門檻所需的最低賠率。"""
    if p <= 0:
        return None
    return (1 + min_ev_pct / 100.0) / p


def round_stake(amount: float, unit: int) -> int:
    unit = max(int(unit or 1), 1)
    return int(math.floor(amount / unit) * unit)


# ---------------------------------------------------------------------------
# 單場分析
# ---------------------------------------------------------------------------
def _sides_of(game: dict) -> list[str]:
    sides = ["away", "home"]
    if game.get("draw_odds"):
        sides.append("draw")
    return sides


def kickoff(game: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{game['date']} {game['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TW)
    except Exception:
        return None


def game_status(game: dict, now: datetime | None = None) -> str:
    if game.get("result"):
        return "settled"
    ko = kickoff(game)
    now = now or datetime.now(TW)
    if ko and now >= ko:
        return "started"
    return "upcoming"


def analyze_game(game: dict, settings: dict, current_bankroll: float | None = None,
                 risk_block: bool = False) -> dict:
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    bankroll = current_bankroll if current_bankroll is not None else s["bankroll"]
    sides = _sides_of(game)
    odds = [float(game.get(f"{sd}_odds") or 0) for sd in sides]
    warnings: list[str] = []

    bad_odds = any(o <= 1 for o in odds)
    if bad_odds:
        warnings.append("尚未輸入運彩賠率（需大於 1.00）")

    imps = [implied(o) for o in odds]
    overround = sum(imps)
    fair = devig(odds, s["devig_method"])

    # 模型勝率：缺值時以市場去水機率代替（=沒有模型優勢）
    raw_model = []
    for i, sd in enumerate(sides):
        v = game.get(f"model_p_{sd}")
        raw_model.append(float(v) / 100.0 if v not in (None, "") else None)
    if any(v is None for v in raw_model):
        warnings.append("部分模型勝率未填，以市場去水機率代替")
        raw_model = [fair[i] if v is None else v for i, v in enumerate(raw_model)]
    msum = sum(raw_model)
    if msum > 0 and abs(msum - 1.0) > 0.005:
        warnings.append(f"模型勝率加總 {msum*100:.1f}%，已自動正規化為 100%")
        raw_model = [v / msum for v in raw_model]
    model = raw_model

    status = game_status(game)
    side_rows = []
    for i, sd in enumerate(sides):
        o = odds[i]
        p = model[i]
        e = ev(p, o) if o > 1 else -1
        k_full = kelly(p, o) if o > 1 else 0
        stake_pct = max(0.0, min(k_full * s["kelly_fraction"], s["max_stake_pct"] / 100.0))
        open_o = game.get(f"open_{sd}_odds")
        side_rows.append({
            "side": sd,
            "label": SIDE_LABEL[sd],
            "team": game.get(f"{sd}_team") if sd != "draw" else "和局",
            "odds": o,
            "open_odds": open_o,
            "odds_move": round(o - float(open_o), 2) if open_o else None,
            "implied_p": round(imps[i] * 100, 2),
            "fair_p": round(fair[i] * 100, 2),
            "model_p": round(p * 100, 2),
            "edge": round((p - fair[i]) * 100, 2),
            "ev": round(e * 100, 2),
            "kelly_full": round(k_full * 100, 2),
            "stake_pct": round(stake_pct * 100, 2),
            "stake_amount": round_stake(bankroll * stake_pct, s["stake_round"]),
            "break_even_p": round(imps[i] * 100, 2),
            "target_odds": round(target_odds(p, s["min_ev"]), 2) if p > 0 else None,
        })

    # 篩選合格的下注選項
    reasons_fail = []
    qualified = []
    for r in side_rows:
        fails = []
        if r["ev"] < s["min_ev"]:
            fails.append(f"EV {r['ev']:+.2f}% 未達 +{s['min_ev']}%")
        if r["edge"] < s["min_edge"]:
            fails.append(f"優勢 {r['edge']:+.2f}% 未達 +{s['min_edge']}%")
        if not (s["min_odds"] <= r["odds"] <= s["max_odds"]):
            fails.append(f"賠率 {r['odds']} 不在 {s['min_odds']}~{s['max_odds']} 區間")
        if r["stake_amount"] <= 0:
            fails.append("建議注金為 0")
        if fails:
            reasons_fail.append({"team": r["team"], "reasons": fails})
        else:
            qualified.append(r)

    rec = None
    if qualified and not bad_odds:
        best = max(qualified, key=lambda r: r["ev"])
        tier = "強" if best["ev"] >= 8 else ("中" if best["ev"] >= 5 else "弱")
        rec = {
            "target": best["side"].upper(),
            "side": best["side"],
            "team_name": best["team"],
            "odds": best["odds"],
            "ev": best["ev"],
            "edge": best["edge"],
            "kelly_pct": best["stake_pct"],
            "stake_amount": best["stake_amount"],
            "tier": tier,
        }
    if rec and status != "upcoming":
        rec["note"] = "已開賽/已結束，僅供參考"
    if rec and risk_block:
        rec["blocked"] = True
        rec["note"] = "今日虧損已達上限，系統暫停建議"

    return {
        **game,
        "status": status,
        "overround": round(overround, 4),
        "vig_pct": round((overround - 1) * 100, 2),
        "payout_pct": round(100 / overround, 2) if overround else 0,
        "sides": side_rows,
        "recommendation": rec,
        "no_bet_reasons": reasons_fail if not rec else [],
        "warnings": warnings,
        "auto_summary": _summary(game, side_rows, rec, s, bad_odds),
    }


def _summary(game, rows, rec, s, bad_odds=False) -> str:
    if not rows:
        return ""
    if bad_odds:
        parts = [f"{r['team']} 勝率 {r['model_p']:.1f}%，運彩賠率需 ≥ {r['target_odds']:.2f}"
                 for r in rows if r["target_odds"]]
        return "請輸入運彩賠率。" + "；".join(parts) + " 才達進場門檻。"
    if rec:
        r = next(x for x in rows if x["side"] == rec["side"])
        return (f"{r['team']} 賠率 {r['odds']:.2f}，市場去水機率 {r['fair_p']:.1f}%，"
                f"模型勝率 {r['model_p']:.1f}%（優勢 {r['edge']:+.1f}%），"
                f"EV {r['ev']:+.2f}% ≥ 門檻 +{s['min_ev']}%，"
                f"以 {s['kelly_fraction']:g} 凱利配置 {r['stake_pct']:.2f}% 資金。")
    best = max(rows, key=lambda x: x["ev"])
    t = best["target_odds"]
    return (f"最佳選項 {best['team']} EV 僅 {best['ev']:+.2f}%，未達 +{s['min_ev']}% 門檻；"
            f"賠率需升至 {t:.2f} 以上才值得進場。" if t else "資料不足，無法判斷。")


# ---------------------------------------------------------------------------
# 串關
# ---------------------------------------------------------------------------
def parlay(legs: list[dict], settings: dict) -> dict:
    """legs: [{odds, p(%), fair_p(%)?}]"""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    odds_total, p_total, fair_total = 1.0, 1.0, 1.0
    has_fair = all(lg.get("fair_p") for lg in legs)
    for lg in legs:
        odds_total *= float(lg["odds"])
        p_total *= float(lg["p"]) / 100.0
        if has_fair:
            fair_total *= float(lg["fair_p"]) / 100.0
    e = ev(p_total, odds_total)
    k_full = kelly(p_total, odds_total)
    stake_pct = max(0.0, min(k_full * s["kelly_fraction"], s["max_stake_pct"] / 100.0))
    return {
        "legs": len(legs),
        "odds": round(odds_total, 3),
        "model_p": round(p_total * 100, 3),
        "fair_p": round(fair_total * 100, 3) if has_fair else None,
        "ev": round(e * 100, 2),
        "kelly_full": round(k_full * 100, 2),
        "stake_pct": round(stake_pct * 100, 2),
        # 若模型沒有優勢（勝率=市場去水機率），串關的期望值；用來顯示抽水被放大的程度
        "market_ev": round(ev(fair_total, odds_total) * 100, 2) if has_fair else None,
    }


# ---------------------------------------------------------------------------
# 下注紀錄統計
# ---------------------------------------------------------------------------
def settle_profit(bet: dict) -> float:
    st = bet.get("status")
    stake = float(bet.get("stake") or 0)
    if st == "won":
        return stake * (float(bet["odds"]) - 1)
    if st == "lost":
        return -stake
    return 0.0  # pending / void / push


def bet_stats(bets: list[dict], start_bankroll: float) -> dict:
    settled = [b for b in bets if b.get("status") in ("won", "lost", "push", "void")]
    decided = [b for b in settled if b["status"] in ("won", "lost")]
    pending = [b for b in bets if b.get("status") == "pending"]
    settled_sorted = sorted(settled, key=lambda b: (b.get("settled_at") or b.get("created_at") or ""))

    staked = sum(float(b["stake"]) for b in decided)
    profit = sum(settle_profit(b) for b in settled)
    expected = sum(float(b["stake"]) * float(b.get("ev_at_bet") or 0) / 100 for b in decided)
    wins = sum(1 for b in decided if b["status"] == "won")
    avg_odds = (sum(float(b["odds"]) for b in decided) / len(decided)) if decided else 0
    exp_wins = sum(float(b.get("model_p") or 0) / 100 for b in decided if b.get("model_p"))

    # 資金曲線 & 最大回撤
    bank = start_bankroll
    peak = bank
    max_dd = 0.0
    curve = [{"label": "起始", "bankroll": round(bank, 0)}]
    for b in settled_sorted:
        bank += settle_profit(b)
        peak = max(peak, bank)
        if peak > 0:
            max_dd = max(max_dd, (peak - bank) / peak)
        curve.append({"label": (b.get("settled_at") or b.get("created_at") or "")[:10],
                      "bankroll": round(bank, 0)})

    # 連勝連敗
    streak, streak_type = 0, None
    for b in reversed(settled_sorted):
        if b["status"] not in ("won", "lost"):
            continue
        if streak_type is None:
            streak_type, streak = b["status"], 1
        elif b["status"] == streak_type:
            streak += 1
        else:
            break

    # CLV（收盤賠率價值）
    clvs = []
    for b in bets:
        co = b.get("closing_odds")
        if co:
            clvs.append((float(b["odds"]) / float(co) - 1) * 100)

    # 分聯盟
    by_league: dict[str, dict] = {}
    for b in decided:
        lg = b.get("league") or "其他"
        d = by_league.setdefault(lg, {"league": lg, "bets": 0, "wins": 0, "staked": 0.0, "profit": 0.0})
        d["bets"] += 1
        d["wins"] += 1 if b["status"] == "won" else 0
        d["staked"] += float(b["stake"])
        d["profit"] += settle_profit(b)
    for d in by_league.values():
        d["roi"] = round(d["profit"] / d["staked"] * 100, 2) if d["staked"] else 0
        d["hit_rate"] = round(d["wins"] / d["bets"] * 100, 1) if d["bets"] else 0
        d["profit"] = round(d["profit"], 0)

    # 今日損益（風控）
    today = datetime.now(TW).strftime("%Y-%m-%d")
    today_profit = sum(settle_profit(b) for b in settled if (b.get("settled_at") or "")[:10] == today)

    return {
        "total_bets": len(bets),
        "settled": len(settled),
        "pending": len(pending),
        "pending_stake": round(sum(float(b["stake"]) for b in pending), 0),
        "wins": wins,
        "losses": len(decided) - wins,
        "hit_rate": round(wins / len(decided) * 100, 1) if decided else 0,
        "expected_hit_rate": round(exp_wins / len(decided) * 100, 1) if decided else 0,
        "staked": round(staked, 0),
        "profit": round(profit, 0),
        "expected_profit": round(expected, 0),
        "roi": round(profit / staked * 100, 2) if staked else 0,
        "avg_odds": round(avg_odds, 2),
        "current_bankroll": round(start_bankroll + profit, 0),
        "max_drawdown": round(max_dd * 100, 2),
        "streak": streak,
        "streak_type": streak_type,
        "avg_clv": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "clv_count": len(clvs),
        "curve": curve,
        "by_league": sorted(by_league.values(), key=lambda d: -d["bets"]),
        "today_profit": round(today_profit, 0),
    }


# ---------------------------------------------------------------------------
# 模型校準 & 回測（使用已填入賽果的賽事）
# ---------------------------------------------------------------------------
def calibration(analyzed_games: list[dict]) -> dict:
    """比較模型與市場（去水機率）的預測準確度。Brier 分數越低越好。"""
    rows = [g for g in analyzed_games if g.get("result") in SIDES and g.get("sides")]
    if not rows:
        return {"n": 0}
    bm = bk = lm = lk = 0.0
    buckets: dict[int, dict] = {}
    for g in rows:
        for r in g["sides"]:
            y = 1.0 if r["side"] == g["result"] else 0.0
            pm = r["model_p"] / 100
            pk = r["fair_p"] / 100
            bm += (pm - y) ** 2
            bk += (pk - y) ** 2
            lm += -math.log(max(min(pm if y else 1 - pm, 1 - 1e-9), 1e-9))
            lk += -math.log(max(min(pk if y else 1 - pk, 1 - 1e-9), 1e-9))
            bi = min(int(pm * 10), 9)
            bd = buckets.setdefault(bi, {"bucket": f"{bi*10}-{bi*10+10}%", "n": 0, "pred": 0.0, "hit": 0.0})
            bd["n"] += 1
            bd["pred"] += pm
            bd["hit"] += y
    n_out = sum(len(g["sides"]) for g in rows)
    cal = []
    for k in sorted(buckets):
        d = buckets[k]
        cal.append({"bucket": d["bucket"], "n": d["n"],
                    "predicted": round(d["pred"] / d["n"] * 100, 1),
                    "actual": round(d["hit"] / d["n"] * 100, 1)})
    return {
        "n": len(rows),
        "brier_model": round(bm / n_out, 4),
        "brier_market": round(bk / n_out, 4),
        "logloss_model": round(lm / n_out, 4),
        "logloss_market": round(lk / n_out, 4),
        "model_beats_market": bm < bk,
        "buckets": cal,
    }


def backtest(analyzed_games: list[dict], settings: dict) -> dict:
    """依照目前的門檻設定，回推已結束賽事若照建議下注的結果。"""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    rows = sorted([g for g in analyzed_games if g.get("result") in SIDES],
                  key=lambda g: (g.get("date", ""), g.get("time", "")))
    bank = float(s["bankroll"])
    flat_profit = 0.0
    n = wins = 0
    staked = 0.0
    details = []
    for g in rows:
        rec = g.get("recommendation")
        if not rec:
            continue
        side = rec["side"]
        r = next(x for x in g["sides"] if x["side"] == side)
        stake = bank * r["stake_pct"] / 100
        won = g["result"] == side
        pnl = stake * (r["odds"] - 1) if won else -stake
        bank += pnl
        staked += stake
        flat_profit += (r["odds"] - 1) if won else -1
        n += 1
        wins += 1 if won else 0
        details.append({"date": g["date"], "match": f"{g['away_team']} @ {g['home_team']}",
                        "pick": r["team"], "odds": r["odds"], "ev": r["ev"],
                        "won": won, "pnl": round(pnl, 0), "bankroll": round(bank, 0)})
    return {
        "games_with_result": len(rows),
        "bets": n,
        "wins": wins,
        "hit_rate": round(wins / n * 100, 1) if n else 0,
        "flat_roi": round(flat_profit / n * 100, 2) if n else 0,
        "kelly_profit": round(bank - float(s["bankroll"]), 0),
        "kelly_roi": round((bank - float(s["bankroll"])) / staked * 100, 2) if staked else 0,
        "details": details,
    }
