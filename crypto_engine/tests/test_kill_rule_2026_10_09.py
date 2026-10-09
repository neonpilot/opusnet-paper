"""Drawdown halt of the BTC trend account, changed 2026-10-09 (see config.py / registry/CHANGELOG.md, trial #46):
10%-from-peak drawdown -> flatten, 72h cooling-off, automatic re-enable with the peak watermark re-based;
ANY resume (automatic or operator) re-bases the peak; stale-data halt and hand-made HALTED stay manual;
idempotent re-runs; nothing backdated."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import pandas as pd

from crypto_engine import config as C
from crypto_engine.ledger import CryptoLedger
from crypto_engine.paper_step import trend_rules

T0 = pd.Timestamp("2026-01-01 20:00")
ALL_IN = lambda D: ({"BTC": 1.0}, {"summary": "all in", "action": "TARGET"})


def ledger(tmp, **kw):
    return CryptoLedger(tmp, mode="live", label="t", **{**trend_rules(), **kw})


def feed(L, prices, start=0, decide=ALL_IN, lag=pd.Timedelta(hours=1, minutes=17)):
    for k, (o, c) in enumerate(prices):
        t = T0 + pd.Timedelta(hours=start + k)
        L.process_hour(t, {"BTC": o}, {"BTC": c}, decide=decide, decision_time=t + lag)
    L.save()
    return L


def kinds(L):
    return [j["kind"] for j in L.journal]


# -0.3%/h slide: no single 24h loss > 10%, drawdown passes -10% after ~36 hours
SLIDE = [100 * 0.997 ** i for i in range(1, 45)]


def dd_halted(tmp):
    L = feed(ledger(tmp), [(100, 100)] * 6)                       # bought at the 01:00 open
    return feed(L, [(a, a) for a in SLIDE], start=6)


def test_config_declares_rule():
    assert C.KILL_MAX_DRAWDOWN == 0.10 and C.KILL_MAX_DD_AUTO and C.KILL_MAX_DD_COOLOFF_HOURS == 72
    assert C.REBASE_PEAK_ON_RESUME and C.KILL_DD_RULE_CHANGED_ON == "2026-10-09"
    assert C.KILL_24H_LOSS == 0.10 and C.KILL_24H_COOLOFF_HOURS == 72 and C.STALE_HOURS == 6
    assert trend_rules() == {"auto_reenable_hours": 72, "auto_dd_hours": 72, "rebase_peak_on_resume": True}


def test_drawdown_halt_is_auto_and_flattens(tmp_path):
    L = dd_halted(tmp_path)
    k = [j for j in L.journal if j["kind"] == "KILL_SWITCH"]
    assert len(k) == 1 and k[0]["reasons"][0].startswith("drawdown") and k[0]["halt_kind"] == "auto"
    assert not any(r.startswith("24h loss") for r in k[0]["reasons"])
    st = L.state
    assert st["halted"] and st["halt_kind"] == "auto" and st["cooloff_hours"] == 72
    acted, breach = pd.Timestamp(st["halted_at_utc"][:-1]), pd.Timestamp(st["halt_breach_candle_close_utc"][:-1])
    assert acted >= breach and pd.Timestamp(st["reenable_at_utc"][:-1]) == acted + pd.Timedelta(hours=72)
    assert (tmp_path / "AUTO_HALTED").exists() and not (tmp_path / "HALTED").exists()
    assert not st["positions"] and not st["pending"]              # flatten filled at a later hourly open
    sells = [j for j in L.journal if j["kind"] == "FILL" and j["side"] == "SELL"]
    assert len(sells) == 1 and pd.Timestamp(sells[0]["fill_bar_open_utc"][:-1]) >= acted


def test_drawdown_auto_reenables_after_72h_and_rebases_peak(tmp_path):
    L = dd_halted(tmp_path)
    ra = pd.Timestamp(L.state["reenable_at_utc"][:-1])
    old_peak, start = L.state["peak_equity"], 6 + len(SLIDE)
    px = SLIDE[-1]
    n_before = int((ra - (T0 + pd.Timedelta(hours=start))) / pd.Timedelta(hours=1))   # closes < reenable_at
    L = feed(L, [(px, px)] * n_before, start=start)
    assert L.state["halted"] and "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)
    L = feed(L, [(px, px)], start=start + n_before)
    ev = [j for j in L.journal if j["kind"] == "KILL_SWITCH_AUTO_REENABLE"]
    assert len(ev) == 1 and pd.Timestamp(ev[0]["ts_utc"][:-1]) >= ra
    assert ev[0]["previous_peak_equity"] == old_peak
    st = L.state
    assert not st["halted"] and "reenable_at_utc" not in st and not (tmp_path / "AUTO_HALTED").exists()
    assert abs(st["peak_equity"] - st["last_equity"]) < 1e-9 and st["peak_equity"] < old_peak * 0.91
    assert not st["positions"] and not st["pending"]              # nothing forced at re-enable
    # no immediate re-halt; next daily decision acts normally
    L = feed(L, [(px, px)] * 30, start=start + n_before + 1)
    assert len([j for j in L.journal if j["kind"] == "KILL_SWITCH"]) == 1
    assert L.state["positions"].get("BTC", 0) > 0


def test_operator_resume_rebases_peak(tmp_path):
    """The 2026-10-08 bug: a manual halt cleared by the operator must restart from current equity, not the old peak."""
    L = feed(ledger(tmp_path), [(100, 100)] * 6)
    L = feed(L, [(120, 120)] * 3, start=6)                        # peak ~120
    peak = L.state["peak_equity"]
    assert peak > 115
    (tmp_path / "HALTED").write_text("operator")
    L = ledger(tmp_path)
    L.sync_manual_halt(T0 + pd.Timedelta(hours=9, minutes=30))
    L = feed(L, [(110, 110)] * 3, start=9)                        # flattened at ~110 (-8% from peak)
    assert L.state["halted"] and not L.state["positions"]
    (tmp_path / "HALTED").unlink()
    L = ledger(tmp_path)
    L = feed(L, [(110, 110)], start=12)
    r = [j for j in L.journal if j["kind"] == "KILL_SWITCH_RESET"]
    eq = L.state["last_equity"]
    assert len(r) == 1 and r[0]["previous_peak_equity"] == peak and r[0]["new_peak_equity"] == eq
    assert L.state["peak_equity"] == eq and not L.state["halted"]
    # re-entering and a further 5% dip (≈ -12% vs the OLD peak) must NOT trip the 10% drawdown rule now
    L = feed(L, [(110, 110)] * 30, start=13)                      # decision on the 23:00 candle, fill 01:00
    assert L.state["positions"].get("BTC", 0) > 0                 # (30h flat so the 24h window is clean)
    L = feed(L, [(104, 104)] * 3, start=43)
    assert L.state["last_equity"] / peak - 1 < -0.10                # would have tripped against the OLD peak
    assert "KILL_SWITCH" not in kinds(L) and not L.state["halted"]


def test_operator_early_clear_of_auto_halt_rebases_peak(tmp_path):
    L = dd_halted(tmp_path)
    (tmp_path / "AUTO_HALTED").unlink()
    L = ledger(tmp_path)
    px = SLIDE[-1]
    L = feed(L, [(px, px)], start=6 + len(SLIDE))
    ev = [j for j in L.journal if j["kind"] == "KILL_SWITCH_AUTO_REENABLE"]
    assert len(ev) == 1 and "removed by operator" in ev[0]["note"]
    assert abs(L.state["peak_equity"] - L.state["last_equity"]) < 1e-9


def test_manual_halted_never_auto_clears(tmp_path):
    L = feed(ledger(tmp_path), [(100, 100)] * 6)
    (tmp_path / "HALTED").write_text("operator")
    L = ledger(tmp_path)
    L.sync_manual_halt(T0 + pd.Timedelta(hours=6, minutes=30))
    assert L.state["halt_kind"] == "manual" and L.state["reenable_at_utc"] is None
    L = feed(L, [(100, 100)] * 240, start=6)                      # 10 days
    assert L.state["halted"] and (tmp_path / "HALTED").exists()
    assert "KILL_SWITCH_AUTO_REENABLE" not in kinds(L) and not L.state["positions"]


def test_manual_file_during_drawdown_auto_halt_cancels_reenable(tmp_path):
    L = dd_halted(tmp_path)
    (tmp_path / "HALTED").write_text("operator")
    L = ledger(tmp_path)
    L.sync_manual_halt(T0 + pd.Timedelta(hours=6 + len(SLIDE)))
    assert L.state["halt_kind"] == "manual" and not (tmp_path / "AUTO_HALTED").exists()
    px = SLIDE[-1]
    L = feed(L, [(px, px)] * 150, start=6 + len(SLIDE))
    assert L.state["halted"] and "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)


def test_stale_data_halt_stays_manual(tmp_path):
    L = feed(ledger(tmp_path), [(100, 100)] * 6)
    L.halt(["stale data: newest completed candle closed long ago"], T0 + pd.Timedelta(hours=14))
    L.save()
    assert L.state["halt_kind"] == "manual" and L.state["reenable_at_utc"] is None
    assert (tmp_path / "HALTED").exists() and not (tmp_path / "AUTO_HALTED").exists()
    L = feed(ledger(tmp_path), [(100, 100)] * 120, start=6)
    assert L.state["halted"] and "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)


def test_auto_hours_classification(tmp_path):
    L = ledger(tmp_path)
    assert L._auto_hours(["drawdown -11% from peak $1 breached the -10% limit"]) == 72
    assert L._auto_hours(["24h loss -11% breached the -10% limit"]) == 72
    assert L._auto_hours(["drawdown x", "24h loss y"]) == 72
    assert L._auto_hours(["drawdown x", "something else"]) is None
    old = CryptoLedger(tmp_path / "o", auto_reenable_hours=72)    # 2026-10-08 configuration
    assert old._auto_hours(["drawdown x"]) is None and old._auto_hours(["drawdown x", "24h loss y"]) == 72


def test_idempotent_reruns(tmp_path):
    L = dd_halted(tmp_path)
    px = SLIDE[-1]
    L = feed(L, [(px, px)] * 90, start=6 + len(SLIDE))           # through the auto re-enable
    snap = lambda: tuple((tmp_path / f).read_text() for f in ("state.json", "journal.jsonl", "equity.csv"))
    before = snap()
    L2 = ledger(tmp_path)
    L2.sync_manual_halt(T0 + pd.Timedelta(hours=300))
    feed(L2, [(100, 100)] * 6 + [(a, a) for a in SLIDE] + [(px, px)] * 90)   # same candles: all skipped
    assert snap() == before and not L2.new_journal
    # during a halt: a re-run does not re-trip, move the times or duplicate the flatten
    p = tmp_path / "b"
    dd_halted(p)
    st0 = json.loads((p / "state.json").read_text())
    L4 = ledger(p)
    feed(L4, [(a, a) for a in SLIDE], start=6)
    L4.sync_manual_halt(T0 + pd.Timedelta(hours=60))
    assert json.loads((p / "state.json").read_text()) == st0


def test_late_run_never_backdates(tmp_path):
    L = feed(ledger(tmp_path), [(100, 100)] * 6)
    L = feed(L, [(a, a) for a in SLIDE], start=6, lag=pd.Timedelta(hours=5, minutes=3))
    st = L.state
    assert pd.Timestamp(st["halted_at_utc"][:-1]) - pd.Timestamp(st["halt_breach_candle_close_utc"][:-1]) \
        == pd.Timedelta(hours=4, minutes=3)
    fills = [j for j in L.journal if j["kind"] == "FILL"]
    assert all(pd.Timestamp(j["fill_bar_open_utc"][:-1]) >= pd.Timestamp(j["order_created_utc"][:-1]) for j in fills)


def test_aggressive_account_rules_unchanged(tmp_path):
    from crypto_engine.aggr_step import CryptoLedger as _  # noqa: F401  (import works)
    L = CryptoLedger(tmp_path, mode="live", kill_max_dd=C.AGGR_KILL_MAX_DRAWDOWN, kill_24h=C.AGGR_KILL_24H_LOSS,
                     rebalance="exact")
    assert L.auto_dd_hours is None and not L.rebase_peak_on_resume and not L._meta
