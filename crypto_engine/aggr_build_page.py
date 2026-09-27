"""Builds crypto-aggressive/index.html (OPUSNET video-style page for paper account #3, mom_top2_1x) from
results/aggr_backtest.json (exported from the /workspace/aggressive research run), results/aggr_replay.json
(hourly-ledger replay of the past year) and the current live.json snapshot. Every number shown comes from those.
  python -m crypto_engine.aggr_build_page [live.json]"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C

LIVE_URL = "https://raw.githubusercontent.com/neonpilot/opusnet-paper/crypto-state/crypto/state_aggr/live.json"
AGGR_DIR = Path("/workspace/aggressive")
BT_PATH = C.RESULTS_DIR / "aggr_backtest.json"


def export_backtest():
    """Copy the numbers the page needs out of the research folder into the repo (reproducible rebuilds)."""
    R = json.loads((AGGR_DIR / "results_full.json").read_text())
    m = R["results"][C.AGGR_NAME]
    ser = pd.read_pickle(AGGR_DIR / "series.pkl")
    r = ser[C.AGGR_NAME]["ret"].loc["2019-01-01":]
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1
    btc = (1 + ser["btc_hold_1x"]["ret"].loc["2019-01-01":]).cumprod()
    wk = eq.index[::7].append(eq.index[-1:]).unique()
    hit = dd[dd <= -C.AGGR_KILL_MAX_DRAWDOWN]
    first = hit.index[0] if len(hit) else None
    halt = None
    if first is not None:
        pk = eq.loc[:first].idxmax()
        halt = {"first_breach": str(first.date()), "peak_date": str(pk.date()), "dd_at_breach": float(dd.loc[first]),
                "equity_1000_at_breach": float(1000 * eq.loc[first]),
                "equity_1000_no_halt_end": float(1000 * eq.iloc[-1]),
                "note": "With the -60% hard stop and no manual reset, the 2019-start backtest would have gone to cash on "
                        f"{first.date()} and missed the 2021 run."}
    out = {"strategy": C.AGGR_NAME, "source": C.AGGR_BACKTEST["source"], "last_bar": str(r.index[-1].date()),
           "n_trials_total": R["n_trials_total"], "sr0_annual": R["sr0_annual"],
           "metrics": {k: m[k] for k in ["cagr_full", "cagr_oos", "cagr_2022on", "sharpe_full", "maxdd_full", "maxdd_oos",
                                         "final_1000_from_2019", "final_1000_from_2021", "final_1000_from_2025_09_26",
                                         "worst_day", "avg_exposure", "cost_drag_yr", "dsr", "years"]},
           "maxdd_date": str(dd.idxmin().date()), "maxdd_peak": str(eq.loc[:dd.idxmin()].idxmax().date()),
           "bootstrap": R["bootstrap"][C.AGGR_NAME], "btc_bootstrap": R["bootstrap"]["btc_hold_1x"],
           "btc_hold": {k: R["results"]["btc_hold_1x"][k] for k in ["cagr_full", "maxdd_full", "years"]},
           "curve": {"d": [str(d.date()) for d in wk], "e": [round(float(1000 * eq.loc[d]), 2) for d in wk],
                     "btc": [round(float(1000 * btc.loc[d]), 2) for d in wk]},
           "halt_sim": halt}
    BT_PATH.write_text(json.dumps(out, indent=1, default=float))
    return out


def replay_view(RP):
    frames = RP["frames"]
    idx = {f["d"]: i for i, f in enumerate(frames)}
    decs, fills = [], []
    for j in RP["journal"]:
        if j["kind"] == "DECISION":
            r = j.get("rationale", {})
            s = r.get("signal") or {}
            decs.append({"i": idx.get(j["bar_date"]), "d": j["bar_date"], "w": j.get("target_weights", {}),
                         "orders": [o["sym"] for o in j.get("orders", [])], "on": s.get("regime_on"),
                         "btc": s.get("btc_close"), "sma": s.get("btc_sma"), "sel": s.get("selection"),
                         "ref": s.get("selection_refresh_day"),
                         "rk": s.get("selection_ranking_90d"), "summary": r.get("summary")})
        elif j["kind"] == "FILL":
            d = j["ts_utc"][:10]
            fills.append({"i": idx.get(d), "d": d, "ts": j["ts_awst"], "t": j["sym"], "side": j["side"], "qty": j["qty"],
                          "px": j["fill_price"], "open": j["ref_open"], "cost": j["cost"], "notional": j["notional"]})
        elif j["kind"] == "KILL_SWITCH":
            pass
    frames = [{k: f[k] for k in ["d", "e", "c", "dd", "halted", "w"]} for f in frames]
    keep = ["start_capital", "first_hour_utc", "last_hour_utc", "final_equity", "total_return", "max_drawdown_hourly",
            "n_fills", "n_decisions", "costs_paid", "halted", "btc_buy_hold_same_window", "cross_check"]
    return {"summary": {k: RP[k] for k in keep} | {"kills": [{"ts_awst": k["ts_awst"], "reasons": k["reasons"]}
                                                             for k in RP["kill_switch_events"]]},
            "frames": frames, "decisions": decs, "fills": fills}


def main():
    BT = export_backtest() if (AGGR_DIR / "results_full.json").exists() else json.loads(BT_PATH.read_text())
    RP = json.loads((C.RESULTS_DIR / "aggr_replay.json").read_text())
    live_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    live = json.loads(live_path.read_text()) if live_path and live_path.exists() else None
    D = {"generated_at": pd.Timestamp.now(tz="Australia/Perth").isoformat(timespec="seconds"),
         "title": C.AGGR_TITLE, "strategy": C.AGGR_NAME, "live_feed_url": LIVE_URL, "live_initial": live,
         "rules": {"top_k": C.AGGR_TOP_K, "lookback": C.AGGR_LOOKBACK_DAYS, "sma": C.AGGR_SMA_DAYS,
                   "refresh_days": C.AGGR_REFRESH_DAYS, "kill_dd": C.AGGR_KILL_MAX_DRAWDOWN, "leverage": C.AGGR_LEVERAGE,
                   "costs": {"BTC": 15, "ETH": 15, "others": 20}},
         "universe": C.SYMBOLS, "data_source": C.DATA_SOURCE, "backtest": BT, "rv": replay_view(RP)}
    tpl = (C.ROOT / "aggr_template.html").read_text()
    html = tpl.replace("/*__DATA__*/null", json.dumps(D, default=float, separators=(",", ":")))
    C.AGGR_PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (C.AGGR_PAGE_DIR / "index.html").write_text(html)
    print("wrote", C.AGGR_PAGE_DIR / "index.html", len(html) // 1024, "KB")


if __name__ == "__main__":
    main()
