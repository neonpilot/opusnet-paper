"""Crypto research: every candidate through the three gates (Critic / Statistician / Risk roles),
logged to the crypto-only trial registry."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C
from .core import (Registry, backtest, deflated_sharpe, drawdown, fold_bounds, psr, size_at_entry, summarize,
                   variant_id, walk_forward)
from .strategies import STRATEGIES, Strategy, sig_hold


def eval_slice(bt):
    return bt.loc[pd.Timestamp(C.EVAL_START):]


def run_variants(s: Strategy, b):
    bts, params, weights = {}, {}, {}
    for p in s.variants():
        vid = variant_id(s.name, s.version, p)
        w = s.target_weights(b, p)
        bts[vid], params[vid], weights[vid] = eval_slice(backtest(w, b)), p, w
    return bts, params, weights


def engine_check(b):
    """A deliberately leaky signal (today's own return) must NOT make money once shifted."""
    cols = ["BTC", "ETH", "LTC"]
    up = (b.close[cols].pct_change() > 0).astype(float) / 3
    shifted = summarize(eval_slice(backtest(up, b)))["sharpe"]
    leaky = summarize(eval_slice(backtest(up, b, execution="no_shift")))["sharpe"]
    return {"ok": bool(leaky > 3 and abs(shifted) < 1.5), "leaky_signal_sharpe_if_unshifted": leaky,
            "leaky_signal_sharpe_as_backtested": shifted}


def truncation_test(s, b, weights, params):
    idx = b.index
    p0 = int(idx.searchsorted(pd.Timestamp(C.EVAL_START)))
    cuts = [idx[int(p)] for p in np.linspace(p0 + 50, len(idx) - 2, C.LEAKAGE_CUTS)]
    bad, checks = [], 0
    for cut in cuts:
        tb = b.truncate(cut)
        for vid, p in params.items():
            wt = s.target_weights(tb, p).iloc[-1]
            wf = weights[vid].loc[cut].reindex(wt.index)
            checks += 1
            if not np.allclose(wt.values, wf.values, atol=1e-9, equal_nan=True):
                bad.append({"cut": str(cut.date()), "variant": p, "max_abs_diff": float(np.nanmax(np.abs(wt.values - wf.values)))})
    return {"ok": not bad, "checks": checks, "cut_dates": [str(c.date()) for c in cuts], "mismatches": bad[:10]}


def btc_benchmarks(b, oos_index):
    """(a) raw 100% BTC buy-and-hold, (b) BTC sized once by the same 1%-risk/20%-cap rule at the OOS start."""
    raw = pd.DataFrame(0.0, index=b.index, columns=C.SYMBOLS)
    raw.loc[raw.index >= oos_index[0] - pd.Timedelta(days=1), "BTC"] = 1.0
    sig = raw.copy()
    raw_bt = backtest(raw, b).reindex(oos_index)
    sized_bt = backtest(size_at_entry(sig, b), b).reindex(oos_index)
    return raw_bt, sized_bt


def run_research(b, note="", registry=None):
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    reg = registry or Registry()
    engine = engine_check(b)
    per = {}
    for s in STRATEGIES:
        bts, params, weights = run_variants(s, b)
        vm = {v: summarize(bt) for v, bt in bts.items()}
        for v in bts:
            reg.record(s.name, s.version, params[v], vm[v], run_id, note)
        per[s.name] = dict(strategy=s, bts=bts, params=params, weights=weights, vm=vm)

    n_trials = reg.n_trials()
    srs = np.array(reg.daily_sharpes())
    var_sr = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    out = {"run_id": run_id, "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "data": b.meta, "universe": C.SYMBOLS, "quote": C.QUOTE, "n_trials_total": n_trials,
           "n_research_runs": reg.n_runs(), "var_sr_daily_across_trials": var_sr, "engine_check": engine,
           "approval_rule": C.APPROVAL_RULE,
           "protocol": {k: getattr(C, k) for k in ["EVAL_START", "WF_TRAIN_DAYS", "WF_TEST_DAYS", "WF_MIN_FOLD_DAYS",
                                                   "GATE_DSR_MIN", "GATE_OOS_SHARPE_MIN", "GATE_OOS_POS_FOLDS_MIN",
                                                   "RISK_PER_TRADE", "MAX_POSITION", "MAX_GROSS", "STOP_ATR_MULT",
                                                   "KILL_MAX_DRAWDOWN", "KILL_24H_LOSS", "STALE_HOURS", "REPLAY_DAYS"]},
           "costs_bps_one_way": {s: C.COST_BPS.get(s, C.DEFAULT_COST_BPS) for s in C.SYMBOLS}, "strategies": []}

    for name, d in per.items():
        s, bts, params, weights, vm = d["strategy"], d["bts"], d["params"], d["weights"], d["vm"]
        best = max(vm, key=lambda v: vm[v]["sharpe"])
        bm = vm[best]
        ds = deflated_sharpe(bm["sharpe_daily"], bm["n_days"], bm["skew"], bm["kurtosis"], n_trials, var_sr)
        own = np.array([vm[v]["sharpe_daily"] for v in vm])
        ds_own = deflated_sharpe(bm["sharpe_daily"], bm["n_days"], bm["skew"], bm["kurtosis"], len(vm),
                                 float(np.var(own, ddof=1)) if len(vm) > 1 else var_sr)
        g2 = {"pass": bool(ds["dsr"] >= C.GATE_DSR_MIN), **ds,
              "psr_vs_zero": psr(bm["sharpe_daily"], 0.0, bm["n_days"], bm["skew"], bm["kurtosis"]),
              "dsr_if_only_own_grid_counted": ds_own["dsr"], "own_grid_size": len(vm),
              "tested_variant": params[best], "tested_sharpe_annual": bm["sharpe"]}
        wf = walk_forward(bts, params, b.index)
        om = wf["oos_metrics"]
        g3 = {"pass": bool(om["sharpe"] >= C.GATE_OOS_SHARPE_MIN and wf["pos_fold_fraction"] >= C.GATE_OOS_POS_FOLDS_MIN),
              "oos_sharpe": om["sharpe"], "pos_fold_fraction": wf["pos_fold_fraction"],
              "n_folds_counted": wf["n_folds_counted"], "mean_train_sharpe": wf["mean_train_sharpe"],
              "oos_is_ratio": om["sharpe"] / wf["mean_train_sharpe"] if wf["mean_train_sharpe"] > 0 else None}
        tt = truncation_test(s, b, weights, params)
        costs_ok = all(C.COST_BPS.get(t, C.DEFAULT_COST_BPS) > 0 for t in s.universe) and float(bts[best]["cost"].sum()) > 0
        diag = {ex: summarize(eval_slice(backtest(weights[best], b, execution=ex)))["sharpe"] for ex in ["next_open", "no_shift"]}
        g1 = {"pass": bool(tt["ok"] and costs_ok and engine["ok"]), "truncation_test": tt, "costs_included": costs_ok,
              "engine_shift_check": engine, "execution_sensitivity_sharpe": diag}

        raw_bt, sized_bt = btc_benchmarks(b, wf["oos"].index)
        rawm, sizedm = summarize(raw_bt), summarize(sized_bt)
        beats = bool(om["sharpe"] > sizedm["sharpe"])
        dd = drawdown(wf["oos"]["ret"])
        trip = dd[dd <= -C.KILL_MAX_DRAWDOWN]
        risk = {"avg_gross_exposure_oos": om["avg_gross_exposure"], "oos_max_drawdown": om["max_drawdown"],
                "kill_switch_would_have_tripped": bool(len(trip)),
                "first_trip_date": str(trip.index[0].date()) if len(trip) else None,
                "worst_day_oos": float(wf["oos"]["ret"].min())}
        crit = []
        if not beats:
            crit.append(f"OOS Sharpe {om['sharpe']:.2f} does not beat risk-sized buy-and-hold BTC over the same "
                        f"days ({sizedm['sharpe']:.2f}); raw 100% BTC: Sharpe {rawm['sharpe']:.2f}, CAGR {rawm['cagr']:.1%}, "
                        f"max DD {rawm['max_drawdown']:.1%}.")
        else:
            crit.append(f"OOS Sharpe {om['sharpe']:.2f} beats risk-sized BTC ({sizedm['sharpe']:.2f}), but CAGR "
                        f"{om['cagr']:.1%} vs raw 100% BTC {rawm['cagr']:.1%}: the edge is risk-adjusted, at "
                        f"~{om['avg_gross_exposure']:.0%} average exposure.")
        if g2["dsr"] < C.GATE_DSR_MIN:
            crit.append(f"Best full-sample Sharpe {bm['sharpe']:.2f} is not distinguishable from the best of "
                        f"{n_trials} random crypto trials (hurdle {ds['sr0_annual']:.2f}): DSR {ds['dsr']:.2f}.")
        if om["cost_drag_per_year"] > 0.005:
            crit.append(f"Costs eat {om['cost_drag_per_year']:.2%} a year (turnover {om['turnover_per_year']:.1f}x).")
        if g3["oos_is_ratio"] is not None and g3["oos_is_ratio"] < 0.5:
            crit.append(f"OOS Sharpe is only {g3['oos_is_ratio']:.0%} of the in-sample Sharpe used to pick parameters.")
        if diag["no_shift"] - diag["next_open"] > 0.5:
            crit.append(f"Without the one-bar shift the Sharpe would read {diag['no_shift']:.2f} instead of "
                        f"{diag['next_open']:.2f}.")
        if risk["kill_switch_would_have_tripped"]:
            crit.append(f"OOS drawdown reached {om['max_drawdown']:.1%}; the 10% kill switch would have halted "
                        f"it on {risk['first_trip_date']} (backtest ignores the switch; live does not).")
        crit.append("Survivorship: the 9 coins are today's surviving majors, picked in 2026 with hindsight "
                    "(LUNA, FTT, EOS etc. are absent), which flatters every long-only crypto backtest.")
        passed = g1["pass"] and g2["pass"] and g3["pass"]
        eq = (1 + wf["oos"]["ret"]).cumprod() * C.START_CAPITAL
        wk = eq.resample("W-SUN").last().dropna()
        beq = (1 + raw_bt["ret"]).cumprod() * C.START_CAPITAL
        out["strategies"].append({
            "name": s.name, "title": s.title, "version": s.version, "mechanism": s.mechanism, "universe": s.universe,
            "grid": s.grid, "n_variants": len(vm), "best_variant": params[best], "full_sample": bm, "oos": om,
            "benchmark_btc_raw_oos": rawm, "benchmark_btc_sized_oos": sizedm, "beats_sized_btc": beats,
            "folds": wf["folds"], "gates": {"g1_leakage": g1, "g2_dsr": g2, "g3_walkforward": g3},
            "risk": risk, "critic": crit, "passed_all": passed, "qualifies": bool(passed and beats),
            "variants": [{"variant_id": v, "params": params[v], "sharpe": vm[v]["sharpe"], "cagr": vm[v]["cagr"],
                          "max_drawdown": vm[v]["max_drawdown"]} for v in vm],
            "oos_curve": {"dates": [str(x.date()) for x in wk.index], "equity": [round(float(x), 2) for x in wk.values],
                          "benchmark": [round(float(x), 2) for x in beq.resample("W-SUN").last().reindex(wk.index).values],
                          "drawdown": [round(float(x), 4) for x in (wk / wk.cummax() - 1).values]}})
    q = [x for x in out["strategies"] if x["qualifies"]]
    out["approved_strategy"] = max(q, key=lambda x: x["gates"]["g2_dsr"]["dsr"])["name"] if q else None
    return out, per


def build_per(b):
    per = {}
    for s in STRATEGIES:
        bts, params, weights = run_variants(s, b)
        per[s.name] = dict(strategy=s, bts=bts, params=params, weights=weights)
    return per
