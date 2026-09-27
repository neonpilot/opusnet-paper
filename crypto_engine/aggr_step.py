"""Live hourly step for paper account #3, AGGRESSIVE: TOP-2 MOMENTUM (mom_top2_1x). SIMULATED MONEY, REAL PRICES.
  python -m crypto_engine.aggr_step --state-dir DIR [--init]
Separate ledger/state folder (crypto/state_aggr on the crypto-state branch). Same ledger mechanics as the
BTC-trend account: every completed 1h candle booked exactly once (idempotent), missed hours caught up, orders
fill at the first hourly OPEN at/after the run that created them (never backdated), sells before buys, costs
15 bps BTC/ETH / 20 bps others per side, no leverage/borrowing/shorts. Daily decision after the 00:00 UTC close
(signal = crypto_engine.aggr_signal, identical to the backtest). Only automatic kill switch: -60% from peak."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .aggr_signal import target_after_close
from .data import build_bars, fetch_klines
from .ledger import CryptoLedger, H, awst, iso
from .paper_step import fetch_hourly, now_utc

LABEL = "crypto-aggr-live"
RISK_RULES = {"leverage": C.AGGR_LEVERAGE, "kill_max_drawdown_from_peak": C.AGGR_KILL_MAX_DRAWDOWN,
              "kill_24h_loss": C.AGGR_KILL_24H_LOSS, "stale_data_flatten": False,
              "reset": "manual only: delete crypto/state_aggr/HALTED on crypto-state (creating that file halts it)",
              "costs_bps_one_way": {"BTC": 15, "ETH": 15, "others": 20}}


def make_ledger(sdir, **kw):
    return CryptoLedger(sdir, kill_max_dd=C.AGGR_KILL_MAX_DRAWDOWN, kill_24h=C.AGGR_KILL_24H_LOSS,
                        rebalance="exact", min_trade_usd=C.AGGR_MIN_TRADE_USD, **kw)


def fetch_daily(now, days=C.AGGR_DATA_DAYS):
    start_ms = int((now - pd.Timedelta(days=days)).tz_localize("UTC").timestamp() * 1000)
    return build_bars({s: fetch_klines(s, "1d", start_ms) for s in C.SYMBOLS}, {"source": C.DATA_SOURCE, "interval": "1d"})


def btc_scan(close):
    c = close["BTC"]
    sma = c.rolling(C.AGGR_SMA_DAYS).mean()
    return {"dates": [str(d.date()) for d in c.index[-90:]], "close": [round(float(x), 2) for x in c.iloc[-90:]],
            "sma": [None if not np.isfinite(x) else round(float(x), 2) for x in sma.iloc[-90:]], "sma_days": C.AGGR_SMA_DAYS}


def log_decision(L, ts, D, target, rat, orders, eq, extra=None):
    L.log("DECISION", ts, {"bar_date": str(pd.Timestamp(D).date()), "action": rat.get("action"),
                           "target_weights": {k: round(float(v), 4) for k, v in target.items() if v > 0},
                           "orders": orders, "equity": eq, "cash": L.state["cash"], "rationale": rat, **(extra or {})})


def write_live(L, sdir: Path, hourly, now, daily_close=None, stale=None):
    summ = L.summary()
    eqp = sdir / "equity.csv"
    eq = pd.read_csv(eqp, index_col=0, parse_dates=True) if eqp.exists() else pd.DataFrame(columns=["equity"])
    prices = {}
    for s in C.SYMBOLS:
        col = hourly.close[s].dropna()
        if len(col):
            t = col.index[-1]
            ref = col.loc[:t - pd.Timedelta(hours=23)]
            prices[s] = {"last": float(col.iloc[-1]), "candle_close_utc": iso(t + H),
                         "chg_24h": float(col.iloc[-1] / ref.iloc[-1] - 1) if len(ref) else None}
    lines = (sdir / "journal.jsonl").read_text().splitlines() if (sdir / "journal.jsonl").exists() else []
    jl = [json.loads(x) for x in lines]
    lastdec = next((j for j in reversed(jl) if j["kind"] == "DECISION"), None)
    daily_eq = eq["equity"][eq.index.hour == 23] if len(eq) else pd.Series(dtype=float)
    nxt = now.normalize() + pd.Timedelta(days=1) + pd.Timedelta(minutes=19)
    prev = json.loads((sdir / "live.json").read_text()) if (sdir / "live.json").exists() else {}
    scan = btc_scan(daily_close) if daily_close is not None else prev.get("btc_scan")
    out = {"kind": "opusnet-crypto-aggr-live", "strategy": C.AGGR_NAME, "title": C.AGGR_TITLE,
           "generated_utc": iso(now), "generated_awst": awst(now), "data_source": C.DATA_SOURCE,
           "account": summ, "prices": prices, "risk_rules": RISK_RULES,
           "equity_hourly": {"t": [iso(t) for t in eq.index[-24 * 14:]], "e": [round(float(x), 2) for x in eq["equity"].iloc[-24 * 14:]]},
           "equity_daily": {"d": [str(t.date()) for t in daily_eq.index], "e": [round(float(x), 2) for x in daily_eq.values]},
           "journal_tail": jl[-40:], "fills": [j for j in jl if j["kind"] == "FILL"][-200:],
           "last_decision": lastdec, "btc_scan": scan, "stale": stale,
           "next_decision_utc": iso(nxt), "notice": "PAPER TRADING - SIMULATED MONEY - REAL PRICES"}
    (sdir / "live.json").write_text(json.dumps(out, default=float, separators=(",", ":")))


def init(sdir: Path, now):
    if (sdir / "state.json").exists():
        sys.exit("state already exists; refusing to re-open the account")
    sdir.mkdir(parents=True, exist_ok=True)
    hourly = fetch_hourly(now - pd.Timedelta(hours=30))
    daily = fetch_daily(now)
    D = daily.index[-1]
    if now - (D + pd.Timedelta(days=1)) >= pd.Timedelta(days=1):
        sys.exit(f"latest daily candle {D.date()} is not the most recent close; refusing to decide on stale data")
    L = make_ledger(sdir, mode="live", label=LABEL, opened_at=now)
    t_last = hourly.index[-1]
    L.state.update(last_hour=iso(t_last), last_daily_bar=str(D.date()), strategy=C.AGGR_NAME, risk_rules=RISK_RULES,
                   last_px={s: float(v) for s, v in hourly.close.ffill().iloc[-1].items() if np.isfinite(v)})
    L.log("ACCOUNT_OPENED", now, {"start_capital": C.START_CAPITAL, "strategy": C.AGGR_NAME, "risk_rules": RISK_RULES,
                                  "note": "aggressive paper account opened (simulated money, real prices)"})
    target, rat = target_after_close(daily.close, D)
    eq = L.state["cash"]
    orders = L._orders(target, eq, L.state["last_px"], now, rat["summary"])
    L.state["pending"] = orders
    L.state["targets"] = {k: float(v) for k, v in target.items() if v > 0}
    log_decision(L, now, D, target, rat, orders, eq, {
        "note": f"first decision at account opening: adopts the position the rule holds for {rat['position_day']} "
                f"(closes to {D.date()}); sized off the latest hourly close; fills at the next hourly open after "
                f"{awst(now)} AWST"})
    L.save()
    write_live(L, sdir, hourly, now, daily.close)
    print("opened", json.dumps(rat, default=float)[:1500])
    print("orders", orders)


def step(sdir: Path, now):
    L = make_ledger(sdir, mode="live")
    hf = L.halted_file()
    if hf.exists() and not L.state["halted"]:
        L.halt(["manual halt: HALTED file created by operator"], now)
    last = pd.Timestamp(L.state["last_hour"].rstrip("Z"))
    hourly = fetch_hourly(min(last + H, now - pd.Timedelta(hours=30)))
    new_hours = hourly.index[hourly.index > last]
    cache = {}

    def decide(D):
        if "daily" not in cache:
            cache["daily"] = fetch_daily(now)
        if D not in cache["daily"].index:
            raise RuntimeError(f"daily candle {D.date()} not yet published; aborting run without saving")
        return target_after_close(cache["daily"].close, D)

    for t in new_hours:
        o = {s: float(v) for s, v in hourly.open.loc[t].items() if np.isfinite(v)}
        c = {s: float(v) for s, v in hourly.close.loc[t].items() if np.isfinite(v)}
        L.process_hour(t, o, c, decide=decide, decision_time=now)
    newest_close = (hourly.index[-1] + H) if len(hourly.index) else last + H
    stale = None
    if now - newest_close > pd.Timedelta(hours=C.STALE_HOURS):
        stale = f"newest completed candle closed {iso(newest_close)}, more than {C.STALE_HOURS}h ago (no flatten; flagged only)"
    if not len(new_hours) and not L.new_journal:
        print("no new completed hourly candle; nothing changed")
        return
    L.save()
    write_live(L, sdir, hourly, now, cache["daily"].close if "daily" in cache else None, stale)
    s = L.summary()
    print(f"processed {len(new_hours)} hour(s) to {L.state['last_hour']}: equity ${s['equity']:.2f} cash ${s['cash']:.2f} "
          f"positions {[(p['sym'], round(p['weight'], 3)) for p in s['positions']]} pending {len(s['pending'])} halted {s['halted']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--init", action="store_true")
    a = ap.parse_args()
    sdir, now = Path(a.state_dir), now_utc()
    (init if a.init else step)(sdir, now)


if __name__ == "__main__":
    main()
