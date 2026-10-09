"""Live 24/7 paper step (runs hourly in GitHub Actions, or on the box).
  python -m crypto_engine.paper_step --state-dir DIR [--init]
Idempotent: every completed hourly candle is processed exactly once (state.last_hour); a run with no new
candle changes nothing. SIMULATED MONEY, REAL PRICES, no exchange keys, no real orders."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .data import MS, build_bars, fetch_klines, load
from .decisions import Decider, load_research
from .ledger import CryptoLedger, H, awst, iso


def trend_rules():
    """Kill-switch options of the BTC trend account (24h rule changed 2026-10-08, drawdown rule and peak
    re-base on resume changed 2026-10-09; see config.py and registry/CHANGELOG.md)."""
    return {"auto_reenable_hours": C.KILL_24H_COOLOFF_HOURS,
            "auto_dd_hours": C.KILL_MAX_DD_COOLOFF_HOURS if C.KILL_MAX_DD_AUTO else None,
            "rebase_peak_on_resume": C.REBASE_PEAK_ON_RESUME}


def now_utc():
    return pd.Timestamp.now(tz="UTC").tz_localize(None).floor("s")


def fetch_hourly(start: pd.Timestamp):
    start_ms = int(start.tz_localize("UTC").timestamp() * 1000)
    frames = {s: fetch_klines(s, "1h", start_ms) for s in C.SYMBOLS}
    return build_bars(frames, {"source": C.DATA_SOURCE, "interval": "1h"})


def btc_scan(daily, params):
    n = (params or {}).get("sma", 100)
    c = daily.close["BTC"]
    sma = c.rolling(n).mean()
    return {"dates": [str(d.date()) for d in c.index[-90:]], "close": [round(float(x), 2) for x in c.iloc[-90:]],
            "sma": [None if not np.isfinite(x) else round(float(x), 2) for x in sma.iloc[-90:]], "sma_days": n}


def write_live(L, sdir: Path, hourly, R, now):
    summ = L.summary()
    eqp = sdir / "equity.csv"
    eq = pd.read_csv(eqp, index_col=0, parse_dates=True) if eqp.exists() else pd.DataFrame(columns=["equity"])
    last = hourly.close.ffill().iloc[-1]
    prices = {}
    for s in C.SYMBOLS:
        col = hourly.close[s].dropna()
        if len(col):
            t = col.index[-1]
            ref = col.loc[:t - pd.Timedelta(hours=23)]
            prices[s] = {"last": float(col.iloc[-1]), "candle_close_utc": iso(t + H),
                         "chg_24h": float(col.iloc[-1] / ref.iloc[-1] - 1) if len(ref) else None}
    jl = [json.loads(x) for x in (sdir / "journal.jsonl").read_text().splitlines()[-60:]] if (sdir / "journal.jsonl").exists() else []
    lastdec = next((j for j in reversed(jl) if j["kind"] == "DECISION"), None)
    daily_eq = eq["equity"][eq.index.hour == 23] if len(eq) else pd.Series(dtype=float)
    nxt = (now.normalize() + pd.Timedelta(days=1) + pd.Timedelta(minutes=C.ORDER_DELAY_MIN))
    out = {"kind": "opusnet-crypto-live", "generated_utc": iso(now), "generated_awst": awst(now),
           "data_source": C.DATA_SOURCE, "approved_strategy": R.get("approved_strategy"), "research_run": R["run_id"],
           "n_trials": R["n_trials_total"], "account": summ, "prices": prices,
           "equity_hourly": {"t": [iso(t) for t in eq.index[-24 * 14:]], "e": [round(float(x), 2) for x in eq["equity"].iloc[-24 * 14:]]},
           "equity_daily": {"d": [str(t.date()) for t in daily_eq.index], "e": [round(float(x), 2) for x in daily_eq.values]},
           "journal_tail": jl[-40:], "last_decision": lastdec, "btc_scan": L.state.get("btc_scan"),
           "next_decision_utc": iso(nxt), "notice": "PAPER TRADING - SIMULATED MONEY - REAL PRICES"}
    (sdir / "live.json").write_text(json.dumps(out, default=float, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--init", action="store_true")
    a = ap.parse_args()
    sdir = Path(a.state_dir)
    R = load_research()
    now = now_utc()
    if a.init:
        if (sdir / "state.json").exists():
            sys.exit("state already exists; refusing to re-open the account")
        sdir.mkdir(parents=True, exist_ok=True)
        hourly = fetch_hourly(now - pd.Timedelta(hours=30))
        daily = load("1d", cache=False)
        L = CryptoLedger(sdir, mode="live", label="crypto-live", opened_at=now, **trend_rules())
        t_last = hourly.index[-1]
        L.state["last_hour"] = iso(t_last)
        L.state["last_daily_bar"] = str(daily.index[-1].date())
        L.state["last_px"] = {s: float(v) for s, v in hourly.close.ffill().iloc[-1].items() if np.isfinite(v)}
        dec = Decider(daily, R, shadows=False)
        p = dec._target(dec.approved, daily.index[-1])[1] if dec.approved else None
        L.state["btc_scan"] = btc_scan(daily, p)
        L.log("ACCOUNT_OPENED", now, {"start_capital": C.START_CAPITAL, "approved_strategy": R.get("approved_strategy"),
                                      "note": f"live paper account opened; first decision at the close of the "
                                              f"{(t_last.normalize()).date() if t_last.hour < 23 else (t_last.normalize() + pd.Timedelta(days=1)).date()} "
                                              f"UTC daily candle (the last completed daily candle {daily.index[-1].date()} was "
                                              f"already a day old, so no decision was forced on it)"})
        L.save()
        write_live(L, sdir, hourly, R, now)
        print("opened", L.summary())
        return

    L = CryptoLedger(sdir, mode="live", **trend_rules())
    L.sync_manual_halt(now)       # a HALTED file created by hand halts the account; never auto-cleared
    last = pd.Timestamp(L.state["last_hour"].rstrip("Z"))
    hourly = fetch_hourly(min(last + H, now - pd.Timedelta(hours=30)))
    new_hours = hourly.index[hourly.index > last]
    cache = {}

    def decide(D):
        if "daily" not in cache:
            cache["daily"] = load("1d", cache=False)
        if D not in cache["daily"].index:
            raise RuntimeError(f"daily candle {D.date()} not yet published; aborting run without saving")
        daily = cache["daily"].truncate(D)
        tgt, rat = Decider(daily, R)(D)
        L.state["btc_scan"] = btc_scan(daily, rat.get("params"))
        return tgt, rat

    for t in new_hours:
        o = {s: float(v) for s, v in hourly.open.loc[t].items() if np.isfinite(v)}
        c = {s: float(v) for s, v in hourly.close.loc[t].items() if np.isfinite(v)}
        L.process_hour(t, o, c, decide=decide, decision_time=now)
    newest_close = (hourly.index[-1] + H) if len(hourly.index) else last + H
    if now - newest_close > pd.Timedelta(hours=C.STALE_HOURS):
        L.halt([f"stale data: newest completed candle closed {iso(newest_close)}, more than {C.STALE_HOURS}h ago"], now)
    if not len(new_hours) and not L.new_journal:
        print("no new completed hourly candle; nothing changed")
        return
    L.save()
    write_live(L, sdir, hourly, R, now)
    s = L.summary()
    print(f"processed {len(new_hours)} hour(s) to {L.state['last_hour']}: equity ${s['equity']:.2f} cash ${s['cash']:.2f} "
          f"positions {[(p['sym'], round(p['weight'], 3)) for p in s['positions']]} pending {len(s['pending'])} halted {s['halted']}")


if __name__ == "__main__":
    main()
