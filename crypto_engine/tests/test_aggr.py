"""Tests for paper account #3 (mom_top2_1x): signal = backtest definition with no look-ahead, ledger idempotence,
fills never backdated, costs applied, only the -60% hard stop."""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np
import pandas as pd
from crypto_engine import config as C
from crypto_engine.aggr_signal import mom_weights, target_after_close, vectorised_returns
from crypto_engine.aggr_step import make_ledger


def synth_close(n=260, seed=1):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    drift = np.linspace(-0.002, 0.004, len(C.SYMBOLS))
    df = pd.DataFrame({s: 100 * np.cumprod(1 + rng.normal(drift[i], 0.03, n)) for i, s in enumerate(C.SYMBOLS)}, index=idx)
    i = np.arange(n)
    df["BTC"] = 100 * np.exp(0.5 * np.sin(2 * np.pi * i / 120)) * np.cumprod(1 + rng.normal(0, 0.01, n))   # regime flips
    return df


# ---------------- signal ----------------
def test_no_lookahead_future_rows_cannot_change_a_decision():
    c = synth_close()
    for D in c.index[150::7]:
        t1, _ = target_after_close(c, D)
        c2 = c.copy()
        c2.loc[c2.index > D] *= np.random.default_rng(3).uniform(0.2, 5.0, (int((c2.index > D).sum()), len(C.SYMBOLS)))
        t2, _ = target_after_close(c2, D)
        assert t1 == t2
        # and the weights held on day t never depend on day t's own close
        w1, _ = mom_weights(c)
        c3 = c.copy(); c3.loc[D + pd.Timedelta(days=1)] *= 3.0
        w3, _ = mom_weights(c3)
        assert np.allclose(w1.loc[:D + pd.Timedelta(days=1)].values, w3.loc[:D + pd.Timedelta(days=1)].values)


def test_decision_equals_full_sample_backtest_row_and_rules():
    c = synth_close()
    W, aux = mom_weights(c)
    n_on = n_ref = 0
    for D in c.index[120:-1]:
        t = D + pd.Timedelta(days=1)
        tgt, rat = target_after_close(c, D)
        exp = {s: v for s, v in W.loc[t].items() if v > 0}
        assert tgt == exp
        assert sum(tgt.values()) in (0.0, 1.0) and all(v <= 0.5 + 1e-12 for v in tgt.values())   # 2 coins, 1x, no leverage
        btc = c["BTC"].loc[:D]
        on = btc.iloc[-1] > btc.rolling(100).mean().iloc[-1]
        assert rat["signal"]["regime_on"] == bool(on) and (bool(tgt) == bool(on))
        if on and (t - pd.Timestamp("2018-01-01")).days % 7 == 0:       # refresh day: top-2 by 90d return to D
            m = (c.loc[D] / c.shift(90).loc[D] - 1).sort_values(ascending=False)
            assert set(tgt) == set(m.index[:2])
            n_ref += 1
        n_on += bool(on)
    assert 10 < n_on < 130 and n_ref >= 3          # the synthetic path exercises both regimes and refresh days


def test_vectorised_costs_charged_on_turnover():
    c = synth_close()
    o = c.shift(1).fillna(100.0)
    v = vectorised_returns(o, c)
    assert (v["cost"] >= 0).all() and (v.loc[v["exposure"] > 0, "cost"] > 0).any()
    assert np.allclose(v["ret"], v["gross"] - v["cost"])


# ---------------- ledger ----------------
T0 = pd.Timestamp("2026-01-04 20:00")          # a Sunday; 23:00 candle closes 00:00 Monday


def feed(L, hours, decide, now_fn):
    for k, (o, c) in enumerate(hours):
        t = T0 + pd.Timedelta(hours=k)
        L.process_hour(t, o, c, decide=decide, decision_time=now_fn(t))


def flat(p):
    return {s: p for s in C.SYMBOLS}


def test_fill_next_open_costs_and_idempotent(tmp_path):
    decide = lambda D: ({"BTC": 0.5, "SOL": 0.5}, {"summary": "top2", "action": "TARGET"})
    hours = [(flat(100), flat(100))] * 4 + [({**flat(100), "BTC": 110, "SOL": 90}, flat(100))] * 3
    L = make_ledger(tmp_path, mode="live", label="t")
    feed(L, hours, decide, lambda t: t + pd.Timedelta(hours=1, minutes=19))
    L.save()
    fills = {j["sym"]: j for j in L.journal if j["kind"] == "FILL"}
    assert set(fills) == {"BTC", "SOL"}
    for s, px, bps in [("BTC", 110, 15), ("SOL", 90, 20)]:
        f = fills[s]
        assert f["fill_bar_open_utc"] == "2026-01-05T01:00:00Z"            # order 00:19 -> first open >= it is 01:00
        assert abs(f["fill_price"] - px * (1 + bps / 1e4)) < 1e-9
        assert abs(f["cost"] - f["qty"] * px * bps / 1e4) < 1e-9
    assert abs(L.state["costs_paid"] - sum(f["cost"] for f in fills.values())) < 1e-9
    assert L.state["cash"] >= -1e-9                                        # never borrows
    snap = {p.name: p.read_text() for p in tmp_path.iterdir()}
    # a re-run over the same candles (e.g. a duplicate cron) changes nothing
    L2 = make_ledger(tmp_path, mode="live")
    feed(L2, hours, decide, lambda t: t + pd.Timedelta(hours=1, minutes=19))
    assert not L2.new_journal and not L2.new_equity
    L2.save()
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == snap


def test_catch_up_never_backdates(tmp_path):
    """A run that arrives 5h late books the missed hours, decides, but fills only at an open after the run."""
    decide = lambda D: ({"ETH": 0.5, "LINK": 0.5}, {"summary": "top2", "action": "TARGET"})
    hours = [(flat(100), flat(100))] * 8
    L = make_ledger(tmp_path, mode="live", label="t")
    run_time = T0 + pd.Timedelta(hours=8, minutes=19)                      # after the last candle closed
    feed(L, hours, decide, lambda t: run_time)
    assert [j for j in L.journal if j["kind"] == "DECISION"]
    assert not [j for j in L.journal if j["kind"] == "FILL"]
    assert all(o["created_at"] == "2026-01-05T04:19:00Z" for o in L.state["pending"])
    L.process_hour(T0 + pd.Timedelta(hours=8), flat(101), flat(101))       # 04:00 candle: open before 04:19 -> no fill
    assert not [j for j in L.journal if j["kind"] == "FILL"]
    L.process_hour(T0 + pd.Timedelta(hours=9), flat(102), flat(102))       # 05:00 open -> fills
    f = [j for j in L.journal if j["kind"] == "FILL"]
    assert len(f) == 2 and all(j["fill_bar_open_utc"] == "2026-01-05T05:00:00Z" and j["ref_open"] == 102 for j in f)


def test_exact_rebalance_and_new_decision_replaces_pending(tmp_path):
    L = make_ledger(tmp_path, mode="live", label="t")
    L.state["positions"] = {"DOGE": 2.0}
    L.state["cash"] -= 200.0
    L.state["pending"] = [{"sym": "BNB", "side": "BUY", "qty": 1, "created_at": "2026-01-09T00:00:00Z"}]  # not yet due
    decide = lambda D: ({"XRP": 0.5, "ADA": 0.5}, {"summary": "top2", "action": "TARGET"})
    feed(L, [(flat(100), flat(100))] * 4, decide, lambda t: t + pd.Timedelta(hours=1, minutes=19))
    P = {o["sym"]: o for o in L.state["pending"]}
    assert set(P) == {"XRP", "ADA", "DOGE"}                        # BNB order replaced; DOGE (not a target) sold
    assert P["DOGE"]["side"] == "SELL" and P["DOGE"].get("close_all")
    assert all(abs(P[s]["qty"] - 0.5 * 1000 / 100) < 1e-9 for s in ("XRP", "ADA"))   # exact target weights


def test_no_24h_switch_only_60pct_hard_stop(tmp_path):
    decide = lambda D: ({"SOL": 0.5, "DOGE": 0.5}, {"summary": "top2", "action": "TARGET"})
    L = make_ledger(tmp_path, mode="live", label="t")
    hours = [(flat(100), flat(100))] * 5 + [(flat(100), flat(100))]         # decision at 23:00, fill 01:00
    hours += [(flat(100), flat(65))]                                         # -35% in one hour: ordinary here
    feed(L, hours, decide, lambda t: t + pd.Timedelta(hours=1, minutes=19))
    assert not L.state["halted"] and L.state["positions"]
    L.process_hour(T0 + pd.Timedelta(hours=7), flat(65), flat(38))          # ~-62% from peak -> hard stop
    assert L.state["halted"] and (tmp_path / "HALTED").exists()
    assert {o["side"] for o in L.state["pending"]} == {"SELL"}
    L.process_hour(T0 + pd.Timedelta(hours=8), flat(38), flat(38))    # 04:00 open is before the 04:17 order
    assert L.state["positions"]
    L.process_hour(T0 + pd.Timedelta(hours=9), flat(38), flat(38))    # 05:00 open -> flattened
    assert not L.state["positions"] and L.state["cash"] > 0
    assert "60%" in [j for j in L.journal if j["kind"] == "KILL_SWITCH"][0]["reasons"][0]
