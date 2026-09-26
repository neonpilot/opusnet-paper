#!/usr/bin/env python
"""Fetch latest Yahoo Finance prices for the OPUSNET universe and mark the PAPER account to market.

Runs in GitHub Actions (see .github/workflows/live-prices.yml). Reads the paper ledger snapshot that the
daily routine publishes in data.json and writes live.json. It NEVER changes positions, places orders or
touches the strategy. Paper trading decisions remain once per day (run_daily.py).
Usage: python scripts/live_prices.py <path/to/data.json> <out/live.json> [previous live.json]
"""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

NY, PERTH = ZoneInfo("America/New_York"), ZoneInfo("Australia/Perth")


def market_state(now_ny, last_bar_date_today: bool) -> str:
    if now_ny.weekday() >= 5:
        return "closed"
    mins = now_ny.hour * 60 + now_ny.minute
    if 9 * 60 + 30 <= mins < 16 * 60:
        # inside regular hours on a weekday; if Yahoo has no bar for today it's an exchange holiday
        return "open" if last_bar_date_today else "closed"
    return "closed"


def main():
    data_path, out_path = sys.argv[1], sys.argv[2]
    prev_path = sys.argv[3] if len(sys.argv) > 3 else None
    data = json.load(open(data_path))
    viz = data.get("viz", {})
    tickers = viz.get("universe") or ["GLD", "SLV", "NVDA", "SPY", "QQQ", "TLT", "IEF", "UUP", "USO", "CPER", "COPX", "BIL"]
    now = datetime.now(timezone.utc)
    now_ny = now.astimezone(NY)

    intraday = yf.download(tickers, period="1d", interval="1m", prepost=False, progress=False,
                           auto_adjust=False, group_by="column", threads=True)
    daily = yf.download(tickers, period="7d", interval="1d", progress=False, auto_adjust=False,
                        group_by="column", threads=True)
    prices, today_bars = {}, False
    for t in tickers:
        last, ts = None, None
        if intraday is not None and not intraday.empty and ("Close", t) in intraday.columns:
            s = intraday[("Close", t)].dropna()
            if len(s):
                last, ts = float(s.iloc[-1]), s.index[-1]
        d = daily[("Close", t)].dropna() if ("Close", t) in daily.columns else pd.Series(dtype=float)
        if last is None and len(d):
            last, ts = float(d.iloc[-1]), d.index[-1]
        if last is None:
            continue
        ts = pd.Timestamp(ts)
        ts = ts.tz_localize(NY) if ts.tzinfo is None else ts.tz_convert(NY)
        # once that session is over, prefer Yahoo's official daily close for the same date
        session_end = ts.normalize() + pd.Timedelta(hours=16)
        if now_ny >= session_end and len(d):
            same = [x for x in d.index if pd.Timestamp(x).date() == ts.date()]
            if same:
                last, ts, official = float(d.loc[same[-1]]), session_end, True
        if ts.date() == now_ny.date():
            today_bars = True
        # previous official close = last daily close strictly before the quote's date
        prev = d[[pd.Timestamp(x).date() < ts.date() for x in d.index]]
        pc = float(prev.iloc[-1]) if len(prev) else None
        prices[t] = {"last": round(last, 4), "time_utc": ts.astimezone(timezone.utc).isoformat(),
                     "time_awst": ts.astimezone(PERTH).strftime("%a %d %b %H:%M"),
                     "prev_close": round(pc, 4) if pc else None,
                     "chg_pct": round(last / pc - 1, 5) if pc else None}
    state = market_state(now_ny, today_bars)

    # mark the published paper ledger to market (read-only)
    live = data.get("live") or {}
    positions = []
    cash = float(live.get("cash", 0.0))
    mark = cash
    for p in live.get("positions", []):
        t, sh = p["ticker"], float(p["shares"])
        px = prices.get(t, {}).get("last", p.get("close"))
        positions.append({"ticker": t, "shares": sh, "last": px, "value": round(sh * px, 2)})
        mark += sh * px
    out = {
        "schema": 1,
        "generated_utc": now.isoformat(timespec="seconds"),
        "generated_awst": now.astimezone(PERTH).strftime("%Y-%m-%d %H:%M"),
        "market": {"state": state, "ny_time": now_ny.strftime("%Y-%m-%d %H:%M"),
                   "note": "US regular session 09:30-16:00 New York" if state == "open" else "US market closed"},
        "source": "Yahoo Finance via yfinance (quotes may be delayed)",
        "prices": prices,
        "account": {"ledger_last_bar": live.get("last_bar"), "cash": round(cash, 2), "positions": positions,
                    "equity_mark": round(mark, 2), "equity_ledger_close": live.get("equity"),
                    "pending_orders": len(live.get("pending", [])),
                    "note": "Read-only mark of the paper ledger; fills are booked by the daily run, not here."},
    }
    if prev_path:
        try:
            prev = json.load(open(prev_path))
            same = prev.get("prices") == out["prices"] and prev.get("market", {}).get("state") == state \
                and prev.get("account", {}).get("equity_mark") == out["account"]["equity_mark"]
            if same:
                print("no change since last update; not writing")
                sys.exit(3)
        except Exception:
            pass
    json.dump(out, open(out_path, "w"), indent=1)
    print(f"wrote {out_path}: market {state}, {len(prices)} prices, paper mark ${mark:.2f}")


if __name__ == "__main__":
    main()
