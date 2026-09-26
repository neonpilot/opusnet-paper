import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np
import pandas as pd
from crypto_engine.core import deflated_sharpe, expected_max_sr, backtest, size_at_entry, fold_bounds
from crypto_engine.data import Bars
from crypto_engine import config as C


def test_dsr_paper_example():
    # Bailey & Lopez de Prado (2014): SR=2.5 annual, T=1250, N=100, V[SR]=0.5, skew=-3, kurt=10 -> DSR ~0.90
    sr = 2.5 / np.sqrt(250); var = 0.5 / 250
    assert abs(deflated_sharpe(sr, 1250, -3, 10, 100, var)["dsr"] - 0.9004) < 0.01


def test_expected_max_grows_with_trials():
    assert expected_max_sr(1000, 1e-4) > expected_max_sr(10, 1e-4) > 0


def toy_bars(n=60, cols=("BTC",)):
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    rng = np.random.default_rng(0)
    close = pd.DataFrame({c: 100 * np.cumprod(1 + rng.normal(0, 0.03, n)) for c in cols}, index=idx)
    open_ = close.shift(1).fillna(100) * (1 + rng.normal(0, 0.001, (n, len(cols))))
    return Bars(open_, close * 1.02, close * 0.98, close, close * 0, {})


def test_backtest_shifts_one_bar():
    b = toy_bars()
    w = pd.DataFrame(0.0, index=b.index, columns=["BTC"])
    w.iloc[10] = 1.0
    bt = backtest(w, b)
    assert bt["ret"].iloc[:11].abs().sum() == 0
    intra = b.close["BTC"].iloc[11] / b.open["BTC"].iloc[11] - 1
    assert abs(bt["ret"].iloc[11] - (intra - 15e-4)) < 1e-12
    gap = b.open["BTC"].iloc[12] / b.close["BTC"].iloc[11] - 1
    assert abs(bt["ret"].iloc[12] - (gap - 15e-4)) < 1e-12


def test_size_at_entry_holds_weight_and_caps():
    b = toy_bars(80, ("BTC", "ETH"))
    sig = pd.DataFrame(0.0, index=b.index, columns=["BTC", "ETH"])
    sig.iloc[30:50] = 1.0
    w = size_at_entry(sig, b)
    assert w.iloc[:30].abs().sum().sum() == 0 and w.iloc[50:].abs().sum().sum() == 0
    assert w["BTC"].iloc[30:50].nunique() == 1          # sized once at entry, held
    assert (w.iloc[30:50] <= C.MAX_POSITION + 1e-12).all().all()
    assert (w.sum(1) <= C.MAX_GROSS + 1e-12).all()


def test_folds_anchor_and_no_overlap():
    idx = pd.date_range("2018-01-01", "2026-09-25", freq="D")
    fb = fold_bounds(idx)
    assert idx[fb[0][0]] == pd.Timestamp(C.EVAL_START)
    for (a0, a1, a2), (b0, b1, b2) in zip(fb, fb[1:]):
        assert a2 == b1 and a1 - a0 == C.WF_TRAIN_DAYS
