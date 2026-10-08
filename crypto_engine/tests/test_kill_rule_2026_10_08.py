"""Kill-switch rule of the BTC trend account, changed 2026-10-08 (see config.py / registry/CHANGELOG.md):
halt + flatten when the rolling-24h loss EXCEEDS 10%, automatic re-enable after a 72h cooling-off,
manual HALTED override never auto-clears, idempotent re-runs, nothing backdated."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import pandas as pd

from crypto_engine import config as C
from crypto_engine.core import health_check
from crypto_engine.ledger import CryptoLedger

T0 = pd.Timestamp("2026-01-01 20:00")
ALL_IN = lambda D: ({"BTC": 1.0}, {"summary": "all in", "action": "TARGET"})


def ledger(tmp):
    # max-drawdown rule off here so each test isolates the 24h rule (the live account keeps it; see test below)
    return CryptoLedger(tmp, mode="live", label="t", kill_max_dd=None, auto_reenable_hours=C.KILL_24H_COOLOFF_HOURS)


def feed(L, prices, start=0, decide=ALL_IN, save=True, decision_lag=pd.Timedelta(hours=1, minutes=17)):
    """prices: list of (open, close) per hour starting at T0 + start hours; decision_time = candle open + lag."""
    for k, (o, c) in enumerate(prices):
        t = T0 + pd.Timedelta(hours=start + k)
        L.process_hour(t, {"BTC": o}, {"BTC": c}, decide=decide, decision_time=t + decision_lag)
    if save:
        L.save()
    return L


def entered(tmp):
    """Hours 20..01: decision on the 23:00 candle, BTC bought at the 01:00 open (100) -> fully invested."""
    return feed(ledger(tmp), [(100, 100)] * 6)


def kinds(L):
    return [j["kind"] for j in L.journal]


def test_config_declares_new_rule():
    assert C.KILL_24H_LOSS == 0.10 and C.KILL_24H_COOLOFF_HOURS == 72
    assert C.KILL_24H_LOSS_ORIGINAL == 0.04 and C.KILL_RULE_CHANGED_ON == "2026-10-08"
    assert C.AGGR_KILL_24H_LOSS is None and C.AGGR_KILL_MAX_DRAWDOWN == 0.60     # aggressive account untouched


def test_health_check_threshold():
    eq = lambda last: pd.Series([1000.0] * 24 + [last])
    assert health_check(eq(910.0), max_dd=None, loss_24h=0.10)["ok"]
    assert health_check(eq(900.0), max_dd=None, loss_24h=0.10)["ok"]          # exactly 10% does not exceed
    assert not health_check(eq(899.0), max_dd=None, loss_24h=0.10)["ok"]


def test_trips_above_10pct_flattens_and_records_times(tmp_path):
    L = entered(tmp_path)
    assert L.state["positions"].get("BTC", 0) > 0
    L = feed(L, [(100, 89)], start=6)                               # -11% within 24h
    k = [j for j in L.journal if j["kind"] == "KILL_SWITCH"]
    assert len(k) == 1 and "24h loss" in k[0]["reasons"][-1]
    st = L.state
    assert st["halted"] and st["halt_kind"] == "auto"
    assert st["halt_breach_candle_close_utc"] == "2026-01-02T03:00:00Z"
    assert st["halted_at_utc"] == "2026-01-02T03:17:00Z"           # time it was acted on, never earlier
    assert st["reenable_at_utc"] == "2026-01-05T03:17:00Z"         # +72h
    assert (tmp_path / "AUTO_HALTED").exists() and not (tmp_path / "HALTED").exists()
    assert [o["side"] for o in st["pending"]] == ["SELL"] and st["pending"][0]["close_all"]
    L = feed(L, [(88, 88)], start=7)                                # 03:00 open is before the 03:17 order
    assert L.state["positions"].get("BTC", 0) > 0
    L = feed(L, [(88, 88)], start=8)                                # flatten fills at the 04:00 open
    assert not L.state["positions"] and L.state["cash"] > 0


def test_does_not_trip_at_9pct(tmp_path):
    L = entered(tmp_path)
    L = feed(L, [(100, 91)] + [(91, 91)] * 30, start=6)
    assert "KILL_SWITCH" not in kinds(L)
    assert not L.state["halted"] and L.state["positions"].get("BTC", 0) > 0


def test_auto_reenables_after_72h_and_waits_for_next_decision(tmp_path):
    L = entered(tmp_path)
    L = feed(L, [(100, 89)], start=6)                               # halt acted on 2026-01-02 03:17
    # 72 more hours in cash: candle closes 04:00 (Jan 2) .. 03:00 (Jan 5) -> still halted (03:00 < 03:17)
    L = feed(L, [(89, 89)] * 72, start=7)
    assert L.state["halted"] and "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)
    hist = [j for j in L.journal if j["kind"] == "DECISION" and j["action"].startswith("HALTED")]
    assert len(hist) == 3                                           # daily decisions during the cooling-off: blocked
    # the candle closing 04:00 on Jan 5 is the first close >= reenable_at (03:17)
    L = feed(L, [(89, 89)], start=79)
    ev = [j for j in L.journal if j["kind"] == "KILL_SWITCH_AUTO_REENABLE"]
    assert len(ev) == 1 and ev[0]["ts_utc"] == "2026-01-05T04:00:00Z"
    st = L.state
    assert not st["halted"] and "reenable_at_utc" not in st and "halt_kind" not in st
    assert not (tmp_path / "AUTO_HALTED").exists()
    assert not st["positions"] and not st["pending"]                # no forced re-entry at re-enable
    # next daily decision (23:00 candle of Jan 5) acts normally; order fills at the following hourly open
    L = feed(L, [(89, 89)] * 22, start=80)                          # candles 04:00 (Jan 5) .. 01:00 (Jan 6)
    dec = [j for j in L.journal if j["kind"] == "DECISION"][-1]
    assert dec["bar_date"] == "2026-01-05" and dec["action"] == "TARGET" and dec["orders"]
    assert L.state["positions"].get("BTC", 0) > 0


def test_manual_halt_file_is_never_auto_cleared(tmp_path):
    L = entered(tmp_path)
    (tmp_path / "HALTED").write_text("operator")
    L = CryptoLedger(tmp_path, mode="live", kill_max_dd=None, auto_reenable_hours=72)
    L.sync_manual_halt(T0 + pd.Timedelta(hours=6, minutes=30))
    assert L.state["halted"] and L.state["halt_kind"] == "manual" and L.state["reenable_at_utc"] is None
    L = feed(L, [(100, 100)] * 200, start=6)                        # > 8 days
    assert L.state["halted"] and (tmp_path / "HALTED").exists()
    assert "KILL_SWITCH_AUTO_REENABLE" not in kinds(L) and not L.state["positions"]
    (tmp_path / "HALTED").unlink()                                  # only the operator clears it
    L = feed(L, [(100, 100)], start=206)
    assert not L.state["halted"] and "KILL_SWITCH_RESET" in kinds(L)


def test_manual_file_during_auto_halt_cancels_auto_reenable(tmp_path):
    L = entered(tmp_path)
    L = feed(L, [(100, 89)], start=6)
    assert L.state["halt_kind"] == "auto"
    (tmp_path / "HALTED").write_text("operator")
    L = CryptoLedger(tmp_path, mode="live", kill_max_dd=None, auto_reenable_hours=72)
    L.sync_manual_halt(T0 + pd.Timedelta(hours=8))
    assert L.state["halt_kind"] == "manual" and L.state["reenable_at_utc"] is None
    assert not (tmp_path / "AUTO_HALTED").exists()
    L = feed(L, [(89, 89)] * 120, start=7)
    assert L.state["halted"] and "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)


def test_legacy_halted_state_without_kind_is_manual(tmp_path):
    """The live state halted on 2026-10-07 under the old rule has no halt_kind and a HALTED file: it must stay
    halted until HALTED is deleted (it is not migrated to an automatic halt)."""
    L = entered(tmp_path)
    L.state.update(halted=True, halt_reasons=["24h loss -4.49% breached the -4% limit"])
    L.save()
    (tmp_path / "HALTED").write_text("{}")
    L = feed(CryptoLedger(tmp_path, mode="live", auto_reenable_hours=72), [(100, 100)] * 100, start=6)
    assert L.state["halted"]
    (tmp_path / "HALTED").unlink()
    L = feed(L, [(100, 100)], start=106)
    r = [j for j in L.journal if j["kind"] == "KILL_SWITCH_RESET"]
    assert len(r) == 1 and not L.state["halted"]


def test_idempotent_reruns(tmp_path):
    L = entered(tmp_path)
    feed(L, [(100, 89)] + [(89, 89)] * 80, start=6)                 # halt + auto re-enable
    snap = lambda: ((tmp_path / "state.json").read_text(), (tmp_path / "journal.jsonl").read_text(),
                    (tmp_path / "equity.csv").read_text())
    before = snap()
    L2 = CryptoLedger(tmp_path, mode="live", kill_max_dd=None, auto_reenable_hours=72)
    L2.sync_manual_halt(T0 + pd.Timedelta(hours=200))               # no HALTED file -> no-op
    feed(L2, [(100, 89)] + [(89, 89)] * 80, start=6)                # same candles again: all skipped
    assert snap() == before and not L2.new_journal
    # a re-run during a halt does not re-trip, move the times, or duplicate the flatten
    p = tmp_path / "b"
    L3 = feed(entered(p), [(100, 89)], start=6)
    st0 = json.loads((p / "state.json").read_text())
    L4 = CryptoLedger(p, mode="live", kill_max_dd=None, auto_reenable_hours=72)
    feed(L4, [(100, 89)], start=6)
    L4.sync_manual_halt(T0 + pd.Timedelta(hours=8))
    assert json.loads((p / "state.json").read_text()) == st0


def test_reenable_never_backdated_in_live_backlog(tmp_path):
    """A run that is late (processes a backlog) stamps the halt with its own run time, so the cooling-off is
    counted from when the halt was really acted on and orders are never created before the run."""
    L = entered(tmp_path)
    run_time = T0 + pd.Timedelta(hours=10, minutes=5)               # the run came 3h late
    L = feed(L, [(100, 89)], start=6, decision_lag=pd.Timedelta(hours=4, minutes=5))
    assert L.state["halt_breach_candle_close_utc"] == "2026-01-02T03:00:00Z"
    assert L.state["halted_at_utc"] == "2026-01-02T06:05:00Z"
    assert L.state["reenable_at_utc"] == "2026-01-05T06:05:00Z"
    assert all(o["created_at"] == "2026-01-02T06:05:00Z" for o in L.state["pending"])


def test_live_account_keeps_drawdown_rule_and_auto_halt_rebases_peak(tmp_path):
    """With the default 10% drawdown rule (as live), a >10% 24h loss also breaches the drawdown rule; the halt
    is still the automatic kind and re-enable re-bases the peak so it does not re-halt at once."""
    L = feed(CryptoLedger(tmp_path, mode="live", label="t", auto_reenable_hours=72), [(100, 100)] * 6)
    L = feed(L, [(100, 89)] + [(89, 89)] * 80, start=6)
    k = [j for j in L.journal if j["kind"] == "KILL_SWITCH"]
    assert len(k) == 1 and k[0]["halt_kind"] == "auto"
    assert "KILL_SWITCH_AUTO_REENABLE" in kinds(L) and not L.state["halted"]
    assert abs(L.state["peak_equity"] - L.state["last_equity"]) < 1e-9
    # a slow 10% drawdown with no single 24h loss > 10% is a MANUAL halt (unchanged rule)
    p = tmp_path / "dd"
    L = feed(CryptoLedger(p, mode="live", label="t", auto_reenable_hours=72), [(100, 100)] * 6)
    px = [100 * (0.997 ** i) for i in range(1, 45)]                 # -0.3%/h: 24h loss ~7%, drawdown > 10%
    L = feed(L, [(a, a) for a in px], start=6)
    k = [j for j in L.journal if j["kind"] == "KILL_SWITCH"]
    assert len(k) == 1 and k[0]["halt_kind"] == "manual" and (p / "HALTED").exists()


def test_accounts_without_auto_reenable_unchanged(tmp_path):
    """auto_reenable_hours=None (aggressive account): a halt writes HALTED with the old content, no new keys."""
    L = CryptoLedger(tmp_path, mode="live", kill_max_dd=None, kill_24h=0.10)
    L = feed(L, [(100, 100)] * 6 + [(100, 89)] + [(89, 89)] * 100)
    assert L.state["halted"] and "halt_kind" not in L.state
    assert set(json.loads((tmp_path / "HALTED").read_text())) == {"halted_at_utc", "reasons"}
    assert "KILL_SWITCH_AUTO_REENABLE" not in kinds(L)
