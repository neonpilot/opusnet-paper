"""Past-year replay of paper account #3 (mom_top2_1x) through the SAME hourly ledger code the live account uses,
on real Binance hourly candles, from $1,000 on the 2025-09-26 position (same start as the backtest's
"$1,000 from 26 Sep 2025"), plus a cross-check against the vectorised backtest over the same days.
  python -m crypto_engine.aggr_replay   ->  crypto_engine/results/aggr_replay.json"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config as C
from .aggr_signal import target_after_close, vectorised_returns
from .aggr_step import make_ledger
from .data import build_bars, fetch_klines
from .ledger import H, iso
from .replay import daily_frames

START_DAY = pd.Timestamp("2025-09-26")          # first position day (decided on the 2025-09-25 close)


def fetch(interval, start):
    ms = int(pd.Timestamp(start).tz_localize("UTC").timestamp() * 1000)
    return build_bars({s: fetch_klines(s, interval, ms) for s in C.SYMBOLS}, {"source": C.DATA_SOURCE, "interval": interval})


def run(hourly, daily, t0, enforce_kill=True):
    L = make_ledger(None, mode="replay", label="aggr-replay" if enforce_kill else "aggr-replay-no-kill", opened_at=t0)
    L.enforce_kill = enforce_kill
    L.log("ACCOUNT_OPENED", t0, {"start_capital": C.START_CAPITAL, "note": "replay of the mom_top2_1x rules"})
    decide = lambda D: target_after_close(daily.close, D)
    snaps = []
    for t in hourly.index[hourly.index >= t0]:
        o = {s: float(v) for s, v in hourly.open.loc[t].items() if np.isfinite(v)}
        c = {s: float(v) for s, v in hourly.close.loc[t].items() if np.isfinite(v)}
        n0 = len(L.journal)
        eq = L.process_hour(t, o, c, decide=decide)
        st = L.state
        snaps.append({"t": t, "e": eq, "c": st["cash"], "peak": st["peak_equity"], "halted": st["halted"],
                      "w": {s: q * st["last_px"][s] / eq for s, q in st["positions"].items()} if eq else {},
                      "kinds": [j["kind"] for j in L.journal[n0:]]})
    return L, snaps


def main():
    hourly = fetch("1h", START_DAY - pd.Timedelta(days=2))
    daily = fetch("1d", START_DAY - pd.Timedelta(days=C.AGGR_DATA_DAYS))
    last_daily = daily.index[-1]
    t0 = START_DAY - H                            # the 23:00 UTC candle of 2025-09-25: first decision on its close
    L, snaps = run(hourly, daily, t0)
    frames = daily_frames(L, snaps)
    eq = np.array([s["e"] for s in snaps])
    vr = vectorised_returns(daily.open, daily.close)
    win = vr.loc[START_DAY:last_daily - pd.Timedelta(days=1)]          # day t return needs open(t+1)
    vec_eq = C.START_CAPITAL * (1 + win["ret"]).cumprod()
    led_daily = pd.Series({pd.Timestamp(f["d"]): f["e"] for f in frames if f["hour_utc"] == 24})
    same = vec_eq.index[-1]
    fills = [j for j in L.journal if j["kind"] == "FILL"]
    decs = [j for j in L.journal if j["kind"] == "DECISION"]
    out = {"generated_at": pd.Timestamp.now(tz="Australia/Perth").isoformat(timespec="seconds"),
           "strategy": C.AGGR_NAME, "start_capital": C.START_CAPITAL, "first_hour_utc": iso(t0),
           "last_hour_utc": iso(snaps[-1]["t"]), "final_equity": float(eq[-1]), "total_return": float(eq[-1] / C.START_CAPITAL - 1),
           "max_drawdown_hourly": float((eq / np.maximum.accumulate(eq) - 1).min()),
           "n_fills": len(fills), "n_decisions": len(decs), "costs_paid": L.state["costs_paid"], "halted": L.state["halted"],
           "kill_switch_events": [j for j in L.journal if j["kind"] == "KILL_SWITCH"],
           "final_positions": L.summary()["positions"],
           "btc_buy_hold_same_window": float(hourly.close["BTC"].iloc[-1] / hourly.open["BTC"].loc[START_DAY + H] - 1),
           "cross_check": {"window": f"{START_DAY.date()}..{same.date()} (equity at the {(same + pd.Timedelta(days=1)).date()} 00:00 UTC mark)",
                           "vectorised_backtest_equity": float(vec_eq.iloc[-1]),
                           "hourly_ledger_equity": float(led_daily.get(same, np.nan)),
                           "research_backtest_equity_to_2026_09_25": C.AGGR_BACKTEST["final_1000_from_2025_09_26"],
                           "note": "ledger fills ~1h after the daily close (01:00 UTC open) instead of the backtest's 00:00 UTC open, "
                                   "sizes off the 23:00 candle close, skips trades under $1"},
           "vectorised_daily": {"d": [str(d.date()) for d in vec_eq.index], "e": [round(float(x), 2) for x in vec_eq]},
           "frames": frames, "journal": L.journal}
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (C.RESULTS_DIR / "aggr_replay.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({k: v for k, v in out.items() if k not in ("frames", "journal", "vectorised_daily")}, indent=1, default=float)[:4000])


if __name__ == "__main__":
    main()
