"""Session replay: the official rules walked hour-by-hour over the past REPLAY_DAYS days of real Binance
candles, starting from $1,000 of simulated money. Also a research shadow without the kill switch and a
cross-check against the vectorised backtest."""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from . import config as C
from .data import load
from .decisions import Decider, load_research
from .ledger import CryptoLedger, H, iso


CURRENT_DD_HOURS = C.KILL_MAX_DD_COOLOFF_HOURS if C.KILL_MAX_DD_AUTO else None


def run_ledger(hourly, decider, t0, enforce_kill=True, label="replay", kill_24h=C.KILL_24H_LOSS,
               auto_reenable_hours=C.KILL_24H_COOLOFF_HOURS, auto_dd_hours=CURRENT_DD_HOURS,
               rebase_peak_on_resume=C.REBASE_PEAK_ON_RESUME):
    """Defaults = the CURRENT official rules (24h switch changed 2026-10-08: >10%, 72h auto re-enable; drawdown
    halt changed 2026-10-09: 10% from peak, 72h auto re-enable, peak re-based at any resume).
    kill_24h=0.10, auto_dd_hours=None reproduces the 2026-10-08 rule; kill_24h=0.04, auto_reenable_hours=None,
    auto_dd_hours=None reproduces the original pre-registered rule."""
    L = CryptoLedger(None, mode="replay", label=label, opened_at=t0, kill_24h=kill_24h,
                     auto_reenable_hours=auto_reenable_hours, auto_dd_hours=auto_dd_hours,
                     rebase_peak_on_resume=rebase_peak_on_resume)
    L.enforce_kill = enforce_kill
    L.log("ACCOUNT_OPENED", t0, {"start_capital": C.START_CAPITAL, "note": "replay of the official rules"})
    snaps = []
    O, Cl = hourly.open, hourly.close
    for t in hourly.index[hourly.index >= t0]:
        o = {s: float(v) for s, v in O.loc[t].items() if np.isfinite(v)}
        c = {s: float(v) for s, v in Cl.loc[t].items() if np.isfinite(v)}
        n0 = len(L.journal)
        eq = L.process_hour(t, o, c, decide=decider)
        st = L.state
        snaps.append({"t": t, "e": eq, "c": st["cash"], "peak": st["peak_equity"], "halted": st["halted"],
                      "w": {s: q * st["last_px"][s] / eq for s, q in st["positions"].items()} if eq else {},
                      "kinds": [j["kind"] for j in L.journal[n0:]]})
    return L, snaps


def daily_frames(L, snaps):
    """One frame per UTC day (the 23:00 candle close = daily close); the last frame is the latest hour."""
    df = pd.DataFrame(snaps).set_index("t")
    rows = [i for i, t in enumerate(df.index) if t.hour == 23] + ([len(df) - 1] if df.index[-1].hour != 23 else [])
    frames, prev_i = [], -1
    j_by_day = {}
    for j in L.journal:
        d = j["ts_utc"][:10]
        j_by_day.setdefault(d, []).append(j)
    for k, i in enumerate(rows):
        t = df.index[i]
        seg = df.iloc[prev_i + 1:i + 1]
        kinds = sorted({x for ks in seg["kinds"] for x in ks})
        e = float(df["e"].iloc[i])
        frames.append({"d": str(t.date()), "hour_utc": t.hour + 1, "e": round(e, 2), "c": round(float(df["c"].iloc[i]), 2),
                       "dd": round(e / float(df["peak"].iloc[i]) - 1, 4), "halted": bool(df["halted"].iloc[i]),
                       "w": {s: round(v, 4) for s, v in df["w"].iloc[i].items() if v > 1e-4}, "k": kinds,
                       "lo": round(float(seg["e"].min()), 2), "hi": round(float(seg["e"].max()), 2)})
        prev_i = i
    return frames


def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    """python -m crypto_engine.replay [--no-refresh] [--end-hour YYYY-MM-DDTHH:00]
    --end-hour pins the window (last processed hourly candle open, UTC) so a rule change can be compared on
    exactly the same days as an earlier replay; daily bars are truncated to the last daily candle that had
    closed by then."""
    R = load_research()
    end_hour = pd.Timestamp(_arg("--end-hour")) if _arg("--end-hour") else None
    refresh = "--no-refresh" not in sys.argv
    daily = load("1d", refresh=refresh)
    back = (C.REPLAY_DAYS + 3) * 24 + (int((pd.Timestamp.now(tz="UTC").tz_localize(None) - end_hour) / H) + 48
                                       if end_hour is not None else 0)
    hourly = load("1h", hours_back=back, refresh=refresh)
    if end_hour is not None:
        hourly = hourly.truncate(end_hour)
        daily = daily.truncate(end_hour.normalize() - pd.Timedelta(days=1) if end_hour.hour < 23 else end_hour.normalize())
    last_daily = daily.index[-1]
    D0 = last_daily - pd.Timedelta(days=C.REPLAY_DAYS - 1)
    t0 = D0 - H                     # the 23:00 UTC candle of the day before D0: first decision on its close
    dec = Decider(daily, R)
    L, snaps = run_ledger(hourly, dec, t0)
    Ls, snaps_s = run_ledger(hourly, dec, t0, enforce_kill=False, label="shadow-no-kill")
    Lo, snaps_o = run_ledger(hourly, dec, t0, label="original-4pct-manual", kill_24h=C.KILL_24H_LOSS_ORIGINAL,
                             auto_reenable_hours=None, auto_dd_hours=None, rebase_peak_on_resume=False)
    Lp, snaps_p = run_ledger(hourly, dec, t0, label="rule-2026-10-08", auto_dd_hours=None,
                             rebase_peak_on_resume=False)
    frames = daily_frames(L, snaps)
    frames_s = daily_frames(Ls, snaps_s)

    # cross-check: vectorised walk-forward returns of the approved strategy over the same daily closes
    xc = None
    if R["approved_strategy"]:
        from .research import run_variants
        from .strategies import BY_NAME
        from .core import params_as_of, backtest
        s = BY_NAME[R["approved_strategy"]]
        bts, params, weights = run_variants(s, daily)
        rets = []
        for D in daily.index[(daily.index >= D0) & (daily.index <= last_daily)]:
            vid, _ = params_as_of(bts, params, daily.index, D - pd.Timedelta(days=1))
            rets.append(float(bts[vid]["ret"].loc[D]))
        vec = float(np.prod(1 + np.array(rets)) - 1)
        led_daily = pd.Series({pd.Timestamp(f["d"]): f["e"] for f in frames_s if f["hour_utc"] == 24})
        led_r = float(led_daily.loc[last_daily] / C.START_CAPITAL - 1) if last_daily in led_daily.index else None
        xc = {"window": f"{D0.date()}..{last_daily.date()}", "vectorised_backtest_return": vec,
              "hourly_ledger_no_kill_return_to_same_close": led_r,
              "note": "differences come from fills ~1h after the daily close (vs the backtest's next open), "
                      "weight drift between resizes, and whole-order rounding"}
    fills = [j for j in L.journal if j["kind"] == "FILL"]
    decs = [j for j in L.journal if j["kind"] == "DECISION"]
    kills = [j for j in L.journal if j["kind"] == "KILL_SWITCH"]
    reen = [j for j in L.journal if j["kind"] == "KILL_SWITCH_AUTO_REENABLE"]
    eq = [s["e"] for s in snaps]
    out = {"generated_at": pd.Timestamp.now(tz="Australia/Perth").isoformat(timespec="seconds"),
           "strategy": R["approved_strategy"], "research_run": R["run_id"], "start_capital": C.START_CAPITAL,
           "first_hour_utc": iso(t0), "last_hour_utc": iso(snaps[-1]["t"]), "final_equity": eq[-1],
           "total_return": eq[-1] / C.START_CAPITAL - 1,
           "max_drawdown_hourly": float(min(np.array(eq) / np.maximum.accumulate(eq) - 1)),
           "n_fills": len(fills), "n_decisions": len(decs), "costs_paid": L.state["costs_paid"],
           "halted": L.state["halted"], "halt_kind": L.state.get("halt_kind"),
           "rule": {"kill_24h_loss": C.KILL_24H_LOSS, "cooloff_hours": C.KILL_24H_COOLOFF_HOURS,
                    "kill_max_drawdown": C.KILL_MAX_DRAWDOWN, "drawdown_auto": C.KILL_MAX_DD_AUTO,
                    "drawdown_cooloff_hours": C.KILL_MAX_DD_COOLOFF_HOURS, "rebase_peak_on_resume": C.REBASE_PEAK_ON_RESUME,
                    "changed_on": C.KILL_DD_RULE_CHANGED_ON,
                    "note": "24h-loss switch changed 2026-10-08 after the live halt of 2026-10-07 (was 4%, manual reset); "
                            "10% drawdown halt changed 2026-10-09 after the live halt of 2026-10-08 (was manual reset): "
                            "72h cooling-off, automatic re-enable, peak re-based at any resume"},
           "n_kill_switch_trips": len(kills),
           "n_auto_halts": sum(1 for k in kills if k.get("halt_kind") == "auto"),
           "n_auto_reenables": len(reen), "auto_reenable_events": reen, "kill_switch_events": kills, "final_positions": L.summary()["positions"],
           "pending_at_end": L.state["pending"],
           "btc_buy_hold_same_window": float(hourly.close["BTC"].iloc[-1] / hourly.open["BTC"].loc[t0 + H] - 1),
           "shadow_no_kill": {"final_equity": snaps_s[-1]["e"], "total_return": snaps_s[-1]["e"] / C.START_CAPITAL - 1,
                              "max_drawdown_hourly": float(min(np.array([s['e'] for s in snaps_s]) /
                                                               np.maximum.accumulate([s['e'] for s in snaps_s]) - 1)),
                              "n_fills": Ls.state["n_fills"]},
           "rule_2026_10_08": {"final_equity": snaps_p[-1]["e"], "total_return": snaps_p[-1]["e"] / C.START_CAPITAL - 1,
                               "max_drawdown_hourly": float(min(np.array([s['e'] for s in snaps_p]) /
                                                                np.maximum.accumulate([s['e'] for s in snaps_p]) - 1)),
                               "n_fills": Lp.state["n_fills"], "halted_at_end": Lp.state["halted"],
                               "kill_switch_events": [{k: j[k] for k in ("ts_utc", "ts_awst", "reasons", "equity")}
                                                      for j in Lp.journal if j["kind"] == "KILL_SWITCH"]},
           "original_rule_4pct_manual": {"final_equity": snaps_o[-1]["e"], "total_return": snaps_o[-1]["e"] / C.START_CAPITAL - 1,
                                         "max_drawdown_hourly": float(min(np.array([s['e'] for s in snaps_o]) /
                                                                          np.maximum.accumulate([s['e'] for s in snaps_o]) - 1)),
                                         "n_fills": Lo.state["n_fills"], "halted_at_end": Lo.state["halted"],
                                         "kill_switch_events": [{k: j[k] for k in ("ts_utc", "ts_awst", "reasons", "equity")}
                                                                for j in Lo.journal if j["kind"] == "KILL_SWITCH"]},
           "cross_check": xc, "frames": frames, "frames_shadow": frames_s, "journal": L.journal,
           "journal_shadow_tail": Ls.journal[-40:]}
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (C.RESULTS_DIR / "replay.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({k: v for k, v in out.items() if k not in ("frames", "frames_shadow", "journal", "journal_shadow_tail")},
                     indent=1, default=float)[:6000])


if __name__ == "__main__":
    main()
