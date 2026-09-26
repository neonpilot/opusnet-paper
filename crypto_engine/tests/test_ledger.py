import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import pandas as pd
from crypto_engine.ledger import CryptoLedger


def run(tmp, prices, decide):
    L = CryptoLedger(tmp, mode="live", label="t")
    t0 = pd.Timestamp("2026-01-01 20:00")
    for k, (o, c) in enumerate(prices):
        L.process_hour(t0 + pd.Timedelta(hours=k), {"BTC": o}, {"BTC": c}, decide=decide,
                       decision_time=t0 + pd.Timedelta(hours=k + 1, minutes=17))
    L.save()
    return L


def test_decision_fills_next_open_with_cost_and_is_idempotent(tmp_path):
    decide = lambda D: ({"BTC": 0.2}, {"summary": "test", "action": "TARGET"})
    prices = [(100, 100)] * 4 + [(110, 110), (111, 111)]      # hours 20..01; 23:00 bar triggers decision
    L = run(tmp_path, prices, decide)
    fills = [j for j in L.journal if j["kind"] == "FILL"]
    assert len(fills) == 1
    f = fills[0]
    assert f["fill_bar_open_utc"] == "2026-01-02T01:00:00Z"      # order at 00:17 -> first open >= it is 01:00
    assert abs(f["fill_price"] - 111 * 1.0015) < 1e-9             # the real open worsened by the 15 bps cost
    assert abs(f["qty"] - 0.2 * 1000 / 100) < 1e-9                # sized off the 23:00 candle close (daily close)
    # idempotent: re-processing an already-processed hour changes nothing
    L2 = CryptoLedger(tmp_path, mode="live")
    before = json.dumps(L2.state, sort_keys=True)
    assert L2.process_hour(pd.Timestamp("2026-01-02 01:00"), {"BTC": 999}, {"BTC": 999}, decide=decide) is None
    assert json.dumps(L2.state, sort_keys=True) == before


def test_kill_switch_flattens_and_blocks(tmp_path):
    decide = lambda D: ({"BTC": 1.0}, {"summary": "all in", "action": "TARGET"})
    prices = [(100, 100)] * 4 + [(100, 100), (100, 100)] + [(100, 80)] + [(80, 80)] * 20 + [(80, 80)] * 3
    L = run(tmp_path, prices, decide)
    kinds = [j["kind"] for j in L.journal]
    assert "KILL_SWITCH" in kinds
    assert L.state["halted"] and not L.state["positions"]
    assert (tmp_path / "HALTED").exists()
    assert all(c > 0 for c in [L.state["cash"]])
