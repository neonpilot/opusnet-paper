"""Builds crypto/index.html (OPUSNET video-style page) from research.json, replay.json and the current
live.json snapshot. Every number shown comes from those files."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .data import load

LIVE_URL = "https://raw.githubusercontent.com/neonpilot/opusnet-paper/crypto-state/crypto/state/live.json"
STOCK_TEMPLATE = Path("/workspace/opusnet/opusnet/video_template.html")


def compact_research(R):
    keep = ["run_id", "generated_at", "n_trials_total", "n_research_runs", "approved_strategy", "approval_rule",
            "protocol", "costs_bps_one_way", "engine_check", "universe", "quote"]
    out = {k: R[k] for k in keep}
    out["data"] = {k: R["data"][k] for k in ["source", "last_bar_open_utc", "fetched_utc"]}
    out["strategies"] = []
    for s in R["strategies"]:
        g = s["gates"]
        out["strategies"].append({
            "name": s["name"], "title": s["title"], "mechanism": s["mechanism"], "n_variants": s["n_variants"],
            "best_variant": s["best_variant"], "passed_all": s["passed_all"], "qualifies": s["qualifies"],
            "beats_sized_btc": s["beats_sized_btc"], "critic": s["critic"],
            "full": {k: s["full_sample"][k] for k in ["sharpe", "cagr", "max_drawdown", "start", "end"]},
            "oos": {k: s["oos"][k] for k in ["sharpe", "cagr", "max_drawdown", "start", "end", "avg_gross_exposure",
                                             "turnover_per_year", "cost_drag_per_year"]},
            "btc_raw": {k: s["benchmark_btc_raw_oos"][k] for k in ["sharpe", "cagr", "max_drawdown"]},
            "btc_sized": {k: s["benchmark_btc_sized_oos"][k] for k in ["sharpe", "cagr", "max_drawdown"]},
            "g1": g["g1_leakage"]["pass"], "g2": g["g2_dsr"]["pass"], "g3": g["g3_walkforward"]["pass"],
            "dsr": g["g2_dsr"]["dsr"], "sr0": g["g2_dsr"]["sr0_annual"],
            "pos_folds": g["g3_walkforward"]["pos_fold_fraction"], "n_folds": g["g3_walkforward"]["n_folds_counted"],
            "folds": [{k: f[k] for k in ["test_start", "test_end", "params", "test_return"]} for f in s["folds"]]})
    return out


def short_fill(j):
    return f"{j['side']} {j['qty']:.6g} {j['sym']} @ {j['fill_price']:,.6g} (cost ${j['cost']:.2f})"


def log_text(j):
    k = j["kind"]
    if k == "DECISION":
        r = j.get("rationale", {})
        return "DEC " + j.get("bar_date", "") + " · " + (r.get("summary") or j.get("action", ""))
    if k == "FILL":
        return "FILL " + short_fill(j)
    if k == "KILL_SWITCH":
        return "KILL SWITCH · " + "; ".join(j["reasons"]) + " → flatten, no new entries" + \
            (f" (automatic halt: re-enables {j['reenable_at_utc']})" if j.get("halt_kind") == "auto" else "")
    if k == "KILL_SWITCH_AUTO_REENABLE":
        return "KILL SWITCH RE-ENABLED · 72h cooling-off over · strategy acts at its next daily decision"
    if k == "ACCOUNT_OPENED":
        return f"ACCOUNT OPENED · ${j['start_capital']:,.0f} simulated"
    return k


def replay_view(RP):
    frames = RP["frames"]
    idx = {f["d"]: i for i, f in enumerate(frames)}
    decs, fills, log = [], [], []
    for j in RP["journal"]:
        d = j["ts_utc"][:10]
        if j["kind"] == "DECISION":
            i = idx.get(j["bar_date"])
            r = j.get("rationale", {})
            decs.append({"i": i, "d": j["bar_date"], "w": j.get("target_weights", {}), "action": j.get("action"),
                         "orders": [{"t": o["sym"], "side": o["side"]} for o in j.get("orders", [])],
                         "sig": r.get("signal"), "params": r.get("params"), "summary": r.get("summary")})
        else:
            i = idx.get(d, idx.get(str((pd.Timestamp(d) - pd.Timedelta(days=1)).date())))
            if j["kind"] == "FILL":
                fills.append({"i": i, "d": d, "t": j["sym"], "side": j["side"], "qty": j["qty"], "px": j["fill_price"],
                              "open": j["ref_open"], "cost": j["cost"], "notional": j["notional"]})
        if j["kind"] != "DECISION" or j.get("orders") or j["seq"] % 7 == 0 or j.get("action", "").startswith("HALT"):
            log.append({"i": idx.get(j.get("bar_date", d), 0) if j["kind"] == "DECISION" else fills[-1]["i"] if j["kind"] == "FILL" else idx.get(d, 0),
                        "seq": j["seq"], "d": d, "kind": j["kind"], "text": log_text(j)})
    return {"sessions": frames, "decisions": decs, "fills": fills, "log": log}


def corr_snaps(daily, frames):
    r = daily.close[C.SYMBOLS].pct_change()
    out = []
    dates = [f["d"] for f in frames]
    for i in list(range(0, len(dates), 14)) + [len(dates) - 1]:
        d = pd.Timestamp(dates[i])
        w = r.loc[:d].iloc[-60:]
        m = w.corr().round(2)
        out.append({"i": i, "d": dates[i], "m": [[None if not np.isfinite(x) else float(x) for x in row] for row in m.values]})
    return out


def main():
    R = json.loads((C.RESULTS_DIR / "research.json").read_text())
    RP = json.loads((C.RESULTS_DIR / "replay.json").read_text())
    live_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    live = json.loads(live_path.read_text()) if live_path and live_path.exists() else None
    daily = load("1d", refresh=False)
    rv = replay_view(RP)
    D = {"generated_at": pd.Timestamp.now(tz="Australia/Perth").isoformat(timespec="seconds"),
         "research": compact_research(R), "live_feed_url": LIVE_URL, "live_initial": live,
         "replay": {k: RP[k] for k in ["strategy", "start_capital", "first_hour_utc", "last_hour_utc", "final_equity",
                                       "total_return", "max_drawdown_hourly", "n_fills", "n_decisions", "costs_paid",
                                       "halted", "btc_buy_hold_same_window", "shadow_no_kill", "cross_check"]}
         | {k: RP.get(k) for k in ["halt_kind", "rule", "n_kill_switch_trips", "n_auto_reenables"]}
         | {"original_rule": RP.get("original_rule_4pct_manual")}
         | {"kill": [{"ts_utc": k["ts_utc"], "ts_awst": k["ts_awst"], "reasons": k["reasons"], "equity": k["equity"]}
                     for k in RP["kill_switch_events"]]},
         "rv": rv, "corr": {"nodes": C.SYMBOLS, "snaps": corr_snaps(daily, RP["frames"])}}
    tpl = (C.ROOT / "page_template.html").read_text()
    html = tpl.replace("/*__DATA__*/null", json.dumps(D, default=float, separators=(",", ":")))
    C.PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (C.PAGE_DIR / "index.html").write_text(html)
    print("wrote", C.PAGE_DIR / "index.html", len(html) // 1024, "KB")


if __name__ == "__main__":
    main()
